import numpy as np
from scipy import stats
import gurobipy as gb
from gurobipy import GRB
from tqdm import tqdm
from typing import Union, Dict, Tuple, List, Optional
import util


def add_1st_stage_variables(mdl: gb.Model,
                            pars: Dict[str, np.ndarray],
                            intvars: bool = False) -> np.ndarray:
    '''
    Creates the 1st stage variables for the current Gurobi model.

    Parameters
    ----------
    mdl : gurobipy.Model
        Model for which to create the 1st stage variables.
    pars : dict[str, int | np.ndarray]
        Dictionary containing the optimization problem parameters.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.

    Returns
    -------
    vars : np.ndarray of gurobipy.Var
        Array containing the 1st stage variables.
    '''
    TYPE = GRB.INTEGER if intvars else GRB.CONTINUOUS
    s, n, m = pars['s'], pars['n'], pars['m']

    vars_ = [('X', (n, s)), ('U', (m, s)), ('Z+', (s - 1,)), ('Z-', (s - 1,))]
    return {
        name: np.array(
            mdl.addVars(*size, lb=0, vtype=TYPE, name=name).values()
        ).reshape(*size) for name, size in vars_
    }


def add_2nd_stage_variables(mdl: gb.Model,
                            pars: Dict[str, np.ndarray],
                            scenarios: int = 1,
                            intvars: bool = False) -> np.ndarray:
    '''
    Creates the 2nd stage variables for the current Gurobi model.

    Parameters
    ----------
    mdl : gurobipy.Model
        Model for which to create the 2nd stage variables.
    pars : dict[str, int | np.ndarray]
        Dictionary containing the optimization problem parameters.
    scenarios : int, optional
        If given, creates a set of 2nd stage variables per scenario. Otherwise,
        it defaults to 1.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.

    Returns
    -------
    vars : np.ndarray of gurobipy.Var
        Array containing the 2nd stage variables.
    '''
    TYPE = GRB.INTEGER if intvars else GRB.CONTINUOUS
    s, n = pars['s'], pars['n']
    size = (n, s) if scenarios == 1 else (scenarios, n, s)

    varnames = ('Y+', 'Y-')
    return {
        name: np.array(
            mdl.addVars(*size, lb=0, vtype=TYPE, name=name).values()
        ).reshape(*size) for name in varnames
    }


def fix_var(mdl: gb.Model, var: np.ndarray, value: np.ndarray) -> None:
    '''
    Fixes a variable to a given value. Not recommended to use this in a loop.

    Parameters
    ----------
    mdl : gurobipy.Model
        The model which the variables belong to.
    var : np.ndarray of gurobipy.Var
        The variable to be fixed.
    value : np.ndarray
        The value at which to fix the variable.
    '''
    var = var.flatten().tolist()
    value = value.flatten().tolist()
    mdl.setAttr(GRB.Attr.LB, var, value)
    mdl.setAttr(GRB.Attr.UB, var, value)


def get_1st_stage_objective(pars: Dict[str, np.ndarray],
                            vars: Dict[str, np.ndarray]
                            ) -> Tuple[Union[gb.LinExpr, float], ...]:
    '''
    Computes the 1st stage objective.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    vars : dict[str, np.ndarray]
        1st stage variables to use to compute the objective. The arrays can be 
        either symbolical, or numerical.

    Returns
    -------
    obj :  gurobipy.LinExpr | float
        An expression (if variables are symbolical) or a number (if vars are
        umerical) representing the 1st stage objective.
    '''
    vars_ = [vars['X'], vars['U'], vars['Z+'], vars['Z-']]
    costs = [pars['C1'], pars['C2'], pars['C3+'], pars['C3-']]
    return gb.quicksum((cost * var).sum() for var, cost in zip(vars_, costs))


def get_2nd_stage_objective(pars: Dict[str, np.ndarray],
                            vars: Dict[str, np.ndarray]
                            ) -> Tuple[Union[gb.LinExpr, float], ...]:
    '''
    Computes the 2nd stage objective.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    vars : dict[str, np.ndarray]
        2nd stage variables to use to compute the objective. The arrays can be 
        either symbolical, or numerical. If the arrays have 3 dimensions, then
        the first dimension is regarded as the number of scenarios.

    Returns
    -------
    obj :  gurobipy.LinExpr | float
        An expression (if variables are symbolical) or a number (if vars are
        umerical) representing the 2nd stage objective.
    '''
    # get the number of scenarios (if 2D, then 1; if 3D, then first dimension)
    S = 1 if vars['Y+'].ndim == 2 else vars['Y+'].shape[0]

    # get variables and costs
    vars_ = [vars['Y+'], vars['Y-']]
    costs = [pars['Q+'], pars['Q-']]

    obj = gb.quicksum((cost * var).sum() for var, cost in zip(vars_, costs))
    return obj / S


def add_1st_stage_constraints(mdl: gb.Model,
                              pars: Dict[str, np.ndarray],
                              vars: Dict[str, np.ndarray]) -> None:
    '''
    Adds 1st stage constraints to the model.

    Parameters
    ----------
    mdl : gurobipy
        Model to add constraints to.
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    vars : dict[str, np.ndarray]
        Dictionary containing the optimization variables.
    '''
    X, U, Zp, Zm = vars['X'], vars['U'], vars['Z+'], vars['Z-']

    # sufficient sources to produce necessary products
    A, B = pars['A'], pars['B']
    con = (A.T @ X - B - U).flatten()
    mdl.addConstrs((con[i] <= 0 for i in range(con.size)), name='con_product')

    # work force level increase/decrease
    con = Zp - Zm - A[:, -1] @ (X[:, 1:] - X[:, :-1])
    mdl.addConstrs((con[i] == 0 for i in range(con.size)), name='con_work')

    # extra capacity upper bounds
    UB = pars['UB']
    con = (U - UB).flatten()
    mdl.addConstrs((con[i] <= 0 for i in range(con.size)), name='con_extracap')


def add_2nd_stage_constraints(mdl: gb.Model,
                              pars: Dict[str, np.ndarray],
                              vars_1st: Dict[str, np.ndarray],
                              vars_2nd: Dict[str, np.ndarray],
                              demands: np.ndarray = None
                              ) -> Optional[gb.MVar]:
    '''
    Adds 2nd stage constraints with some deterministic values in place of 
    random demand variables to the model.

    Parameters
    ----------
    mdl : gurobipy.Model
        Model to add constraints to.
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    vars_1st : dict[str, np.ndarray]
        Dictionary containing the 1st stage optimization variables.
    vars_2nd : dict[str, np.ndarray]
        Dictionary containing the 2nd stage optimization variables.
    demands : np.ndarray, optional
        Deterministic demand values. If None, new variables are added to the 
        model and returned.

    Returns
    -------
    demands : np.ndarray of gurobipy.MVar
        The new demand variables used in the constraints. Only created and 
        returned when no demand is passed in the arguments.
    '''
    # get the number of scenarios (if 2D, then 1; if 3D, then first dimension)
    S = 1 if vars_2nd['Y+'].ndim == 2 else vars_2nd['Y+'].shape[0]

    s, n = pars['s'], pars['n']
    X, Yp, Ym = vars_1st['X'], vars_2nd['Y+'], vars_2nd['Y-']

    if demands is None:
        return_ = True
        size = (n, s) if S == 1 else (S, n, s)
        demands = np.array(
            mdl.addVars(*size, lb=0, ub=0, name='demand').values()
        ).reshape(size)
    else:
        return_ = False

    # in the first period, zero surplus is assumed
    Ym = np.concatenate((np.zeros((*Ym.shape[:-1], 1)), Ym), axis=-1)
    con = (X + Ym[..., :-1] + Yp - Ym[..., 1:] - demands).flatten()
    mdl.addConstrs((con[i] == 0 for i in range(con.size)), name='con_demand')

    if return_:
        return demands


def optimize_EV(
    pars: Dict[str, np.ndarray],
    intvars: bool = False,
    verbose: int = 0
) -> Tuple[float, Dict[str, np.ndarray], Dict[str, np.ndarray]]:
    '''
    Computes the Expected Value solution via a Gurobi model.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.
    verbose : int, optional
        Verbosity level of Gurobi model. Defaults to 0, i.e., no verbosity.

    Returns
    -------
    obj : float
        Value of the objective function at the optimal point.
    solution : dict[str, np.ndarray]
        Dictionary containing the value of each variable at the optimum.
    '''
    # initialize model
    mdl = gb.Model(name='EV')
    if verbose < 1:
        mdl.Params.LogToConsole = 0

    # create the variables
    vars1 = add_1st_stage_variables(mdl, pars, intvars=intvars)
    vars2 = add_2nd_stage_variables(mdl, pars, intvars=intvars)

    # get the 1st and 2nd stage objectives, and sum them up
    mdl.setObjective(get_1st_stage_objective(pars, vars1) +
                     get_2nd_stage_objective(pars, vars2), GRB.MINIMIZE)

    # add 1st stage constraints
    add_1st_stage_constraints(mdl, pars, vars1)

    # to get the EV Solution, in the second stage constraints the random
    # variables are replaced with their expected value
    d_mean = pars['demand_mean']
    if intvars:
        d_mean = d_mean.astype(int)
    add_2nd_stage_constraints(mdl, pars, vars1, vars2, demands=d_mean)

    # run optimization
    mdl.optimize()

    # retrieve optimal objective value and optimal variables
    objval = mdl.ObjVal
    convert = ((lambda o: util.var2val(o).astype(int))
               if intvars else
               (lambda o: util.var2val(o)))
    sol1 = {name: convert(var) for name, var in vars1.items()}
    sol2 = {name: convert(var) for name, var in vars2.items()}
    mdl.dispose()
    return objval, sol1, sol2


def optimize_EEV(pars: Dict[str, np.ndarray],
                 EV_vars1: Dict[str, np.ndarray],
                 samples: np.ndarray,
                 intvars: bool = False,
                 verbose: int = 0) -> List[float]:
    '''
    Computes the expected value of the Expected Value solution via multiple 
    Gurobi models.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    EV_vars1 : dict[str, np.ndarry]
        Dictionary containing the solution to the EV problem.
    samples : np.ndarray
        Array of different samples/scenarios approximating the demand 
        distributions.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.
    verbose : int, optional
        Verbosity level of Gurobi model. Defaults to 0, i.e., no verbosity.

    Returns
    -------
    objs : list[float]
        A list containing the optimal objective for each scenario.
    '''
    # create a starting model for the first scenario. Instead of instantiating
    # a new one, the following scenarios will use it again
    mdl = gb.Model(name='EEV')
    if verbose < 2:
        mdl.Params.LogToConsole = 0

    # create only 2nd variables, 1st stage variables are taken from the EV sol
    vars2 = add_2nd_stage_variables(mdl, pars, intvars=intvars)

    # get numerical 1st stage and symbolical 2nd stage objectives
    mdl.setObjective(get_1st_stage_objective(pars, EV_vars1) +
                     get_2nd_stage_objective(pars, vars2), GRB.MINIMIZE)

    # add only 2nd stage constraints, as 1st were already handled by the EV.
    # The "demands" variable will be de facto used as a parameter of the
    # optimization, as it will be fixed with lb=ub
    demands = add_2nd_stage_constraints(mdl, pars, EV_vars1, vars2)

    # solve each scenario
    results = []
    S = samples.shape[0]  # number of scenarios
    for i in tqdm(range(S), total=S, desc='solving EEV'):
        # set demands to the i-th sample
        fix_var(mdl, demands, samples[i])

        # run optimization and save its result
        mdl.optimize()
        if mdl.Status != GRB.INFEASIBLE:
            results.append(mdl.ObjVal)

    mdl.dispose()
    return results


def optimize_TS(pars: Dict[str, np.ndarray],
                samples: np.ndarray,
                intvars: bool = False,
                verbose: int = 0) -> Tuple[float, Dict[str, np.ndarray]]:
    '''
    Computes the approximated Two-stage Recourse Model, where the continuous 
    distribution is discretazed via sampling.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    samples : np.ndarray
        Samples approximating the continuous distribution to a discrete one.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.
    verbose : int, optional
        Verbosity level of Gurobi model. Defaults to 0, i.e., no verbosity.

    Returns
    -------
    obj : float
        Value of the objective function at the optimal point.
    solution : dict[str, np.ndarray]
        Dictionary containing the value of 1st stage variable at the optimum.
    purchase_prob : float
        The probability, according to the TS solution and the sample, that a 
        purchase from an external source must be done.
    '''
    # get the number of scenarios
    S = samples.shape[0]

    # create large scale deterministic equivalent problem
    mdl = gb.Model(name='LSDE')
    if verbose < 1:
        mdl.Params.LogToConsole = 0

    # create 1st stage variables and 2nd stage variables, one per scenario
    vars1 = add_1st_stage_variables(mdl, pars, intvars=intvars)
    vars2 = add_2nd_stage_variables(mdl, pars, scenarios=S, intvars=intvars)

    # set objective
    obj1 = get_1st_stage_objective(pars, vars1)
    obj2 = get_2nd_stage_objective(pars, vars2)
    mdl.setObjective(obj1 + obj2, GRB.MINIMIZE)

    # set constraints
    add_1st_stage_constraints(mdl, pars, vars1)
    add_2nd_stage_constraints(mdl, pars, vars1, vars2, samples)

    # solve
    mdl.optimize()

    # return the solution
    objval = mdl.ObjVal
    convert = ((lambda o: util.var2val(o).astype(int))
               if intvars else
               (lambda o: util.var2val(o)))
    sol1 = {name: convert(var) for name, var in vars1.items()}
    sol2 = {name: convert(var) for name, var in vars2.items()}

    # compute the purchase probability
    purchase_prob = (sol2['Y+'] > 0).sum() / sol2['Y+'].size

    # return
    mdl.dispose()
    return objval, sol1, purchase_prob


def run_MRP(pars: Dict[str, np.ndarray],
            solution: dict[str, np.ndarray],
            sample_size: int,
            alpha: float = 0.95,
            replicas: int = 30,
            intvars: bool = False,
            verbose: int = 0,
            seed: int = None) -> float:
    '''
    Applies the MRP to a given solution to compute its confidence interval.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    solution : dict[str, np.ndarry]
        Dictionary containing a solution to the problem.
    sample_size : int
        Size of the samples to draw.
    alpha : flaot, optiona
        Confidence percentage for the MRP.
    replicas : int
        Number of replications for the MRP.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.
    verbose : int, optional
        Verbosity level of Gurobi model. Defaults to 0, i.e., no verbosity.
    seed : int
        Random seed for LHS.

    Returns
    -------
    CI : float
        An upper bound on the optimality gap of the given solution.
    '''
    # using the MRP basically mean computing N times the LSTDE
    # create large scale deterministic equivalent problem
    n, s = pars['n'], pars['s']
    S = sample_size
    mdl = gb.Model(name='LSDE')
    if verbose < 2:
        mdl.Params.LogToConsole = 0

    # create 1st stage variables and 2nd stage variables, one per scenario
    vars1 = add_1st_stage_variables(mdl, pars, intvars=intvars)
    vars2 = add_2nd_stage_variables(mdl, pars, scenarios=S, intvars=intvars)

    # set objective
    obj1 = get_1st_stage_objective(pars, vars1)
    obj2 = get_2nd_stage_objective(pars, vars2)
    mdl.setObjective(obj1 + obj2, GRB.MINIMIZE)

    # set constraints
    add_1st_stage_constraints(mdl, pars, vars1)
    demands = add_2nd_stage_constraints(mdl, pars, vars1, vars2)

    # create also a submodel to solve only the second stage problem
    sub = gb.Model(name='sub')
    if verbose < 2:
        sub.Params.LogToConsole = 0

    # add 2nd stage variables and only X from 1st stage
    sub_X = np.array(
        sub.addVars(n, s, lb=0, ub=0, name='X').values()).reshape(n, s)
    sub_vars2 = add_2nd_stage_variables(sub, pars, intvars=intvars)

    # set objective, only 2nd stage
    sub.setObjective(get_2nd_stage_objective(pars, sub_vars2), GRB.MINIMIZE)

    # set constraints, only 2nd stage
    sub_demand = add_2nd_stage_constraints(sub, pars, {'X': sub_X}, sub_vars2)

    # start the MRP
    G = []
    for _ in tqdm(range(replicas), total=replicas, desc='MRP iteration'):
        # draw a sample
        sample = util.draw_samples(S, pars, asint=intvars, seed=seed)

        # fix the problem's demands to this sample
        fix_var(mdl, demands, sample)

        # solve the problem
        mdl.optimize()
        vars1_k = {name: util.var2val(var) for name, var in vars1.items()}

        # calculate G_k
        G_k = 0
        for i in tqdm(range(S), total=S, desc='Computing G  ', leave=False):
            # solve v(w_k_s, x_hat)
            fix_var(sub, sub_demand, sample[i])
            fix_var(sub, sub_X, solution['X'])
            sub.optimize()
            a = get_1st_stage_objective(pars, solution).getValue() + sub.ObjVal

            # solve v(w_k_s, x_k)
            fix_var(sub, sub_X, vars1_k['X'])
            b = get_1st_stage_objective(pars, vars1_k).getValue() + sub.ObjVal

            # accumulate in G_k
            G_k += (a - b) / S

        G.append(G_k)

    mdl.dispose()
    sub.dispose()

    # compute the confidence interval
    G = np.array(G)
    G_bar = G.mean()
    sG = np.sqrt(1 / (replicas - 1) * np.square(G - G_bar).sum())
    sG_ = G.std(ddof=1)
    eps = stats.t.ppf(alpha, replicas - 1) * sG / np.sqrt(replicas)
    return G_bar + eps


def optimize_WS(pars: Dict[str, np.ndarray],
                samples: np.ndarray,
                intvars: bool = False,
                verbose: int = 0) -> List[float]:
    '''
    Computes the Wait-and-See solution.

    Parameters
    ----------
    pars : dict[str, np.ndarray]
        Dictionary containing the optimization problem parameters.
    samples : np.ndarray
        Samples approximating the continuous distribution to a discrete one.
    intvars : bool, optional
        Some of the variables are constrained to integers. Otherwise, they are 
        continuous.
    verbose : int, optional
        Verbosity level of Gurobi model. Defaults to 0, i.e., no verbosity.

    Returns
    -------
    objs : list[float]
        A list with all the objectives for each sample.
    '''
    # create the wait-and-see model
    mdl = gb.Model(name='LSDE')
    if verbose < 1:
        mdl.Params.LogToConsole = 0

    # create 1st and 2nd stage variables
    vars1 = add_1st_stage_variables(mdl, pars, intvars=intvars)
    vars2 = add_2nd_stage_variables(mdl, pars, intvars=intvars)

    # set objective (now we optimize over vars1 as well, whereas in EEV we
    # used the EV solution)
    mdl.setObjective(get_1st_stage_objective(pars, vars1) +
                     get_2nd_stage_objective(pars, vars2), GRB.MINIMIZE)

    # set constraints
    add_1st_stage_constraints(mdl, pars, vars1)
    demands = add_2nd_stage_constraints(mdl, pars, vars1, vars2)

    # solve each scenario
    results = []
    S = samples.shape[0]  # number of scenarios
    for i in tqdm(range(S), total=S, desc='solving WS'):
        # set demands to the i-th sample
        fix_var(mdl, demands, samples[i])

        # run optimization and save its result
        mdl.optimize()
        if mdl.Status != GRB.INFEASIBLE:
            results.append(mdl.ObjVal)
    return results