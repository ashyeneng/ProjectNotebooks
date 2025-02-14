from warnings import warn
from math import inf, isinf
from enum import Enum
import re

class Status(Enum):
    """ Enumeration of possible solution status. """
    OPTIMAL = 'Optimal'
    UNKNOWN = 'Unknown'
    SUBOPTIMAL = 'Suboptimal'
    UNBOUNDED = 'Unbounded'
    INFEASIBLE = 'Infeasible'
    INF_OR_UNB = 'Infeasible or Unbounded'
    

class ReactionType(Enum):
    """ Enumeration of possible reaction types. """
    ENZYMATIC = 'enzymatic'
    TRANSPORT = 'transport'
    EXCHANGE = 'exchange'
    SINK = 'sink'
    OTHER = 'other'


class CommunitySolution(object):

    def __init__(self, community, values):
        self.community = community
        self.values = values
        self.growth = self.abundance = self.exchange = self.internal = self.normalized = self._exchange_map = None
        self.parse_values()

    def __str__(self):
        growth = f"Community growth: {self.growth}\n"
        abundance = "\n".join(f"{org_id}\t{val}" for org_id, val in self.abundance.items())
        return growth + abundance

    def __repr__(self):
        return str(self)

    @property
    def exchange_map(self):
        if self._exchange_map is None:
            self._exchange_map = self.compute_exchanges()

        return self._exchange_map

    def parse_values(self):
        model = self.community.merged_model
        reaction_map = self.community.reaction_map

        self.growth = self.values[model.biomass_reaction]

        self.abundance = {}
        for org_id, organism in self.community.organisms.items():
            growth_i = self.community.reaction_map[(org_id, organism.biomass_reaction)]
            self.abundance[org_id] = self.values[growth_i] / self.growth

        self.exchange = {r_id: self.values[r_id]
                         for r_id in model.get_exchange_reactions()}

        self.internal = {}
        self.normalized = {}

        for org_id, organism in self.community.organisms.items():

            abundance = self.abundance[org_id]

            fluxes = {r_id: self.values[reaction_map[(org_id, r_id)]]
                      for r_id in organism.reactions
                      if (org_id, r_id) in reaction_map}

            rates = {r_id: fluxes[r_id] / abundance if abundance > 0 else 0
                     for r_id in organism.reactions if r_id in fluxes}

            self.internal[org_id] = fluxes
            self.normalized[org_id] = rates

    # calculate overall exchanges (organism x metabolite) -> rate

    def compute_exchanges(self):
        model = self.community.merged_model
        reaction_map = self.community.reaction_map
        exchanges = {}

        for m_id in model.get_external_metabolites():

            for org_id, organism in self.community.organisms.items():
                rate = 0
                if m_id not in organism.metabolites:
                    continue

                for r_id in organism.get_metabolite_reactions(m_id):
                    if (org_id, r_id) not in reaction_map:
                        continue

                    flux = self.values[reaction_map[(org_id, r_id)]]

                    if flux != 0:
                        coeff = organism.reactions[r_id].stoichiometry[m_id]
                        rate += coeff*flux

                if rate != 0:
                    exchanges[(org_id, m_id)] = rate

        return exchanges

    def cross_feeding(self, as_df=False, abstol=1e-6):
        exchanges = self.compute_exchanges()
        cross_all = []

        for m_id in self.community.merged_model.get_external_metabolites():
            r_out = {x: r for (x, m), r in exchanges.items() if m == m_id and r > abstol}
            r_in = {x: -r for (x, m), r in exchanges.items() if m == m_id and -r > abstol}

            total_in = sum(r_in.values())
            total_out = sum(r_out.values())
            total = max(total_in, total_out)

            if total_in > total_out:
                r_out[None] = total_in - total_out
            if total_out > total_in:
                r_in[None] = total_out - total_in

            cross = [(o1, o2, m_id, r1 * r2 / total) for o1, r1 in r_out.items() for o2, r2 in r_in.items()]
            cross_all.extend(cross)

        if as_df:
            from pandas import DataFrame
            cross_all = DataFrame(cross_all, columns=["donor", "receiver", "compound", "rate"])

        return cross_all

    def mass_flow(self, element=None, as_df=False, abstol=1e-6):

        def get_mass(x):
            met = self.community.merged_model.metabolites[x]
            formula = met.metadata.get('FORMULA', '')
            mw = molecular_weight(formula, element=element)
            return 0.001 * mw

        entities = list(self.community.organisms) + [None]
        flow = {(o1, o2): 0 for o1 in entities for o2 in entities}

        for o1, o2, m_id, rate in self.cross_feeding():
            flow[(o1, o2)] += get_mass(m_id) * rate

        flow = {key: val for key, val in flow.items() if val > abstol}

        if as_df:
            from pandas import DataFrame
            flow = [(o1, o2, val) for (o1, o2), val in flow.items()]
            flow = DataFrame(flow, columns=["donor", "receiver", "flow"])

        return flow

    def print_external_fluxes(self, pattern=None, sort=False, abstol=1e-9):
        print_values(self.exchange, pattern=pattern, sort=sort, abstol=abstol)

    def print_internal_fluxes(self, org_id, normalized=False, pattern=None, sort=False, abstol=1e-9):
        if normalized:
            print_values(self.normalized[org_id], pattern=pattern, sort=sort, abstol=abstol)
        else:
            print_values(self.internal[org_id], pattern=pattern, sort=sort, abstol=abstol)

    def print_external_balance(self, m_id, sort=False, percentage=False, equations=False, abstol=1e-9):

        print_balance(self.values, m_id, self.community.merged_model, sort=sort, percentage=percentage, equations=equations,
                      abstol=abstol)

    def print_exchanges(self, m_id=None, abstol=1e-9):

        model = self.community.merged_model
        exchange_rxns = set(model.get_exchange_reactions())

        if m_id:
            mets = [m_id]
        else:
            mets = model.get_external_metabolites()

        for m_id in mets:

            entries = []

            ext_rxns = set(model.get_metabolite_reactions(m_id)) & exchange_rxns

            for r_id in ext_rxns:

                flux = self.values[r_id]
                coeff = model.reactions[r_id].stoichiometry[m_id]
                rate = coeff*flux

                if rate > abstol:
                    entries.append(('=> *   ', "in", rate))
                elif rate < -abstol:
                    entries.append(('   * =>', "out", rate))

            for org_id in self.community.organisms:

                if (org_id, m_id) not in self.exchange_map:
                    continue

                rate = self.exchange_map[(org_id, m_id)]
                if rate > abstol:
                    entries.append(('O --> *', org_id, rate))
                elif rate < -abstol:
                    entries.append(('* --> O', org_id, rate))

            if entries:
                print(m_id)
                entries.sort(key=lambda x: x[2])

                for (sense, org_id, rate) in entries:
                    print(f'[ {sense} ] {org_id:<12} {rate:< 10.6g}')




def build_problem(community, growth=1, bigM=1000):

    solver = community.solver
    model = community.merged_model

    # create biomass variables
    for org_id in community.organisms:
        solver.add_variable(f"x_{org_id}", 0, 1, update=False)

    # create all community reactions
    for r_id, reaction in model.reactions.items():
        if reaction.reaction_type == ReactionType.EXCHANGE:
            solver.add_variable(r_id, reaction.lb, reaction.ub, update=False)
        else:
            lb = -inf if reaction.lb < 0 else 0
            ub = inf if reaction.ub > 0 else 0
            solver.add_variable(r_id, lb, ub, update=False)

    solver.update()

    # sum biomass = 1
    solver.add_constraint("abundance", {f"x_{org_id}": 1 for org_id in community.organisms},
                          rhs=1, update=False)

    # S.v = 0
    table = model.metabolite_reaction_lookup()
    for m_id in model.metabolites:
        solver.add_constraint(m_id, table[m_id], update=False)

    # organism-specific constraints
    for org_id, organism in community.organisms.items():

        for r_id, reaction in organism.reactions.items():
            if (org_id, r_id) in community.reaction_map:
                new_id = community.reaction_map[(org_id, r_id)]

                # growth = mu * X
                if r_id == organism.biomass_reaction:
                    solver.add_constraint(f"g_{org_id}", {f"x_{org_id}": growth, new_id: -1}, update=False)
                # lb * X < R < ub * X
                else:
                    lb = -bigM if isinf(reaction.lb) else reaction.lb
                    ub = bigM if isinf(reaction.ub) else reaction.ub

                    if lb != 0:
                        solver.add_constraint(f"lb_{new_id}", {f"x_{org_id}": lb, new_id: -1}, '<', 0, update=False)

                    if ub != 0:
                        solver.add_constraint(f"ub_{new_id}", {f"x_{org_id}": ub, new_id: -1}, '>', 0, update=False)

    solver.update()

    def update_growth(value):
        # TODO: find a solution that is not CPLEX specific
        coefficients = [(f"g_{x}", f"x_{x}", value) for x in community.organisms]
        solver.problem.linear_constraints.set_coefficients(coefficients)
    solver.update_growth = update_growth

    return solver


def print_values(value_dict, pattern=None, sort=False, abstol=1e-9):

    values = [(key, value) for key, value in value_dict.items() if abs(value) > abstol]

    if pattern:
        re_expr = re.compile(pattern)
        values = [x for x in values if re_expr.search(x[0]) is not None]

    if sort:
        values.sort(key=lambda x: x[1])

    entries = (f'{r_id:<12} {val: .6g}'for (r_id, val) in values)

    print('\n'.join(entries))


def print_balance(values, m_id, model, sort=False, percentage=False, equations=False, abstol=1e-9):
    inputs = model.get_metabolite_producers(m_id)
    outputs = model.get_metabolite_consumers(m_id)

    fwd_in = [(r_id, model.reactions[r_id].stoichiometry[m_id] * values[r_id], '--> o')
              for r_id in inputs if values[r_id] > 0]
    rev_in = [(r_id, model.reactions[r_id].stoichiometry[m_id] * values[r_id], 'o <--')
              for r_id in outputs if values[r_id] < 0]
    fwd_out = [(r_id, model.reactions[r_id].stoichiometry[m_id] * values[r_id], 'o -->')
               for r_id in outputs if values[r_id] > 0]
    rev_out = [(r_id, model.reactions[r_id].stoichiometry[m_id] * values[r_id], '<-- o')
                for r_id in inputs if values[r_id] < 0]

    flux_in = [x for x in fwd_in + rev_in if x[1] > abstol]
    flux_out = [x for x in fwd_out + rev_out if -x[1] > abstol]

    if sort:
        flux_in.sort(key=lambda x: x[1], reverse=True)
        flux_out.sort(key=lambda x: x[1], reverse=False)

    if percentage:
        turnover = sum([x[1] for x in flux_in])
        flux_in = [(x[0], x[1] / turnover, x[2]) for x in flux_in]
        flux_out = [(x[0], x[1] / turnover, x[2]) for x in flux_out]
        print_format = '[ {} ] {:<12} {:< 10.2%}'
    else:
        print_format = '[ {} ] {:<12} {:< 10.6g}'

    if equations:
        print_format += '\t{}'
        lines = (print_format.format(x[2], x[0], x[1], model.reactions[x[0]].to_equation())
                 for x in flux_in + flux_out)
    else:
        lines = (print_format.format(x[2], x[0], x[1]) for x in flux_in + flux_out)

    print('\n'.join(lines))
    


def SteadyCom(community, constraints=None, solver=None):
    if solver is None:
        solver = build_problem(community)

    objective = {community.merged_model.biomass_reaction: 1}

    sol = binary_search(solver, objective, minimize=False, constraints=constraints)

    solution = CommunitySolution(community, sol.values)
    solution.solver = solver  # the CommunitySolution object does not have a solver attribute? Does CommunitySolution need to be included at all? Replace with MSCommunity

    return solution


def SteadyComVA(community, obj_frac=1.0, constraints=None, solver=None):
    if solver is None:
        solver = build_problem(community)

    objective = {community.merged_model.biomass_reaction: 1}

    sol = binary_search(solver, objective, constraints=constraints)
    growth = obj_frac * sol.values[community.merged_model.biomass_reaction]
    solver.update_growth(growth)

    variability = {org_id: [None, None] for org_id in community.organisms}

    for org_id in community.organisms:
        sol2 = solver.solve({f"x_{org_id}": 1}, minimize=True, get_values=False, constraints=constraints)
        variability[org_id][0] = sol2.fobj

    for org_id in community.organisms:
        sol2 = solver.solve({f"x_{org_id}": 1}, minimize=False, get_values=False, constraints=constraints)
        variability[org_id][1] = sol2.fobj

    return variability


def binary_search(solver, objective, obj_frac=1, minimize=False, max_iters=20, abs_tol=1e-3, constraints=None):

    previous_value = 0
    value = 1
    fold = 2
    feasible = False
    last_feasible = 0

    for i in range(max_iters):
        diff = value - previous_value

        if diff < abs_tol:
            break

        if feasible:
            last_feasible = value
            previous_value = value
            value = fold*diff + value
        else:
            if i > 0:
                fold = 0.5
            value = fold*diff + previous_value

        solver.update_growth(value)
        sol = solver.solve(objective, get_values=False, minimize=minimize, constraints=constraints)

        feasible = sol.status == Status.OPTIMAL

    if feasible:
        solver.update_growth(obj_frac * value)
    else:
        solver.update_growth(obj_frac * last_feasible)

    sol = solver.solve(objective, minimize=minimize, constraints=constraints)

    if i == max_iters - 1:
        warn("Max iterations exceeded.")

    return sol