"Network flow computations."

import networkx as nx
from decimal import Decimal as D

from django.conf import settings

from cc.account.models import CreditLine
from cc.ripple import SCALE  # Number of decimal places in amounts.
from cc.payment.mincost import min_cost_flow

COST_SCALE_FACTOR = 1000000

class PaymentError(Exception):
    "Base class for all payment exceptions."
    pass

class NoRoutesError(PaymentError):
    "No possible routes between payer and recipient."
    pass

class InsufficientCreditError(PaymentError):
    "Not enough max flow between payer and recipient to make payment."
    pass


class FlowGraph(object):
    def __init__(self, payer, recipient, ignore_balances=False):
        "Takes payer and recipient nodes."
        self.payer = payer
        self.recipient = recipient
        self.graph = build_graph(self.payer, ignore_balances)
        
    def min_cost_flow(self, amount):
        """
        Determine minimum cost route for given amount between payer and
        recipient.
        
        Raises NoRoutesError if there is no route from payer to recipient.
        Raises InsufficientCreditError if the network cannot support the
        specified flow amount.
        """
        self._set_endpoint_demand(amount)
        if self.recipient.alias not in self.graph.nodes():
            raise NoRoutesError()
        try:
            _, flow_dict = min_cost_flow(self.graph)
        except nx.NetworkXUnfeasible:
            raise InsufficientCreditError()
        else:
            amounts = creditline_amounts(flow_dict, self.graph)
            return amounts

    def max_flow(self):
        if (self.recipient.alias not in self.graph.nodes() or
            self.payer.alias not in self.graph.nodes()):
            return 0
        try:
            amount = nx.max_flow(
                unmulti(self.graph), self.payer.alias, self.recipient.alias)
        except nx.NetworkXUnbounded:
            return D('Infinity')
        else:
            return unscale_flow_amount(amount)
            
    def _set_endpoint_demand(self, amount):
        "Add payer and recipient nodes with corresponding demands values."
        self.graph.node[self.payer.alias]['demand'] = scale_flow_amount(-amount)
        self.graph.node[self.recipient.alias]['demand'] = (
            scale_flow_amount(amount))


def build_graph(seed_node, ignore_balances=False):
    """
    Get flow graph for performing payment computations.

    A flow graph is a connected directed networkx DiGraph where the edges
    represent account-halves and exchanges between them performed by various
    users.  Payment always flows from credit line owner to the other
    partner.

    A flow graph contains all credit lines that could possibly be used to
    transfer value from seed_node to anyone else.  It may also contain other
    account-halves so it can be cached and used for other payments.  For
    example, the flow graph might contain the set of all nodes that could
    pay or be paid by the payer.

    If ignore_balances is True, existing account balances are ignored (assumed
    to be zero), allowing for flow calculations based only credit limits,
    which can be used for reputation metrics.
    
    The flow graph assigns costs to each credit line edge in order prioritize
    settling balances.  If ignore_balances is True, costs are all zero.

    To use this graph in the min cost demand flow algorithm, assign the payer
    a supply (negative demand) and the recipient a demand equal to the payment
    amount.

    Flow graph nodes are node aliases.
    """
    graph = nx.MultiDiGraph()
    visited_creditline_ids = {}  # Indexed by node id.
    pending_creditlines = list(seed_node.out_creditlines())
    while pending_creditlines:
        curr_creditline = pending_creditlines.pop(0)

        # Add creditline edge(s) to graph.
        add_creditline_to_graph(graph, curr_creditline, ignore_balances)
        visited_creditline_ids.setdefault(
            curr_creditline.node_id, set()).add(curr_creditline.id)

        # Add partner's unvisited outgoing credit lines to pending
        # list for eventual visitation.
        partner = curr_creditline.partner
        next_creditlines = partner.out_creditlines().exclude(
            pk__in=visited_creditline_ids.get(partner.id, []))
        pending_creditlines += list(next_creditlines)
    return graph

def add_creditline_to_graph(graph, creditline, ignore_balances):
    src = creditline.node.alias
    dest = creditline.partner.alias
    if ignore_balances:
        chunks = [(scale_flow_amount(creditline.limit), 0)]
    else:
        chunks = edge_data(creditline)
    for i, chunk in enumerate(chunks):
        capacity, weight = chunk
        if capacity > 0:  # Don't add capacity 0 edges.
            graph.add_edge(src, dest, key=i, weight=weight,
                           creditline=creditline)
            # Infinite capacity is indicated by not adding a capacity.
            if capacity != D('Infinity'):
                graph[src][dest][i]['capacity'] = capacity

def edge_data(creditline):
    """
    Assigns a cost to using this creditline in a payment.  Cashing in existing
    IOUs has zero cost.  Issuing existing IOUs has a cost of 1 + distance of
    current balance from zero relative to the limit.

    Returns a tuple of (capacity, weight) tuples representing chunks of usable
    credit capacity, since when there is a positive balance, using up that
    balance will carry different weight than anything beyond.

    (TODO: Ideally, costs would be in proportion to the amount of credit
    remaining *after* the payment, but that is not known yet, and the naive min
    cost demand flow algorithm used can't factor that in.)
    """
    if creditline.limit is None:
        # No cost if no limit -- treat as if balance is always 0.
        data = ((D('Infinity'), 0),)  # Capacity is infinite.
    else:
        if creditline.balance > 0:
            # Return two chunks: one to get to zero balance, one for remainder.
            # Give positive cost only to issuing in new IOUs.
            data = ((creditline.balance, 0), (creditline.limit, 1))
        else:
            # No positive balance to cash in.
            capacity = creditline.balance + creditline.limit
            if creditline.limit != 0:
                cost = 1.0 + (float(creditline.balance / creditline.limit))
            else:
                cost = 0
            data = ((capacity, cost),)
    return scale_edge_data(data)

def scale_edge_data(edge_data):
    "Scale capacities and costs to ints."
    new_data = []
    for capacity, cost in edge_data:
        new_data.append((scale_flow_amount(capacity),
                         int(cost * COST_SCALE_FACTOR)))
    return new_data

def creditline_amounts(flow_dict, graph):
    """
    Returns a list of (creditline, amount) tuples that represent the
    flow of a payment. Takes a flow_dict from min_cost_flow.
    """
    amount_dict = {}  # Index by creditline.
    for src_node, node_flow_dict in flow_dict.items():
        for dest_node, edge_dict in node_flow_dict.items():
            creditline = graph[src_node][dest_node][0].get('creditline')
            for amount in edge_dict.values():
                amount = unscale_flow_amount(amount)
                if amount == 0:  # Ignore zero amounts.
                    continue
                amount_dict.setdefault(creditline, 0)
                amount_dict[creditline] += amount
    return amount_dict.items()

def scale_flow_amount(amount):
    "Convert amount decimal to int for min cost flow algorithm."
    if amount != D('Infinity'):
        amount = int(amount * 10**SCALE)
    return amount

def unscale_flow_amount(amount):
    "Convert scaled flow amount int back to decimal."
    return D(amount) / D('1' + '0' * SCALE)

def unmulti(G):
    # Convert multidigraph to regular digraph by inserting nodes in the middle
    # of each edge.
    H = nx.DiGraph()
    for n, data in G.nodes(data=True):
        H.add_node(n, **data)
    for u, v, k, data in G.edges(keys=True, data=True):
        i = '%s__%s__%s' % (u, v, k)
        H.add_edge(u, i, **data)
        H.add_edge(i, v)
    return H
