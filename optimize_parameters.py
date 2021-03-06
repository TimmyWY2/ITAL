import sys, math
from collections import OrderedDict

import numpy as np
import numexpr as ne
from sklearn.model_selection import KFold, StratifiedKFold
from sklearn.metrics import average_precision_score, mean_squared_error
from tqdm import tqdm, trange

import utils
from datasets import RegressionDataset, MultitaskRetrievalDataset
from ital.gp import GaussianProcess



default_grids = { 'full' : OrderedDict((
    ('length_scale', [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 15., 20., 25.]),
    ('var', [0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0]),
    ('noise', [1e-8, 1e-6, 1e-4, 1e-3, 1e-2, 0.05, 0.1])
)), 'ls_only' : OrderedDict((
    ('length_scale', [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 15., 20., 25.]),
))}

default_init = { 'length_scale' : 0.1, 'var' : 1.0, 'noise' : 1e-6 }



def cross_validate_gp(dataset, relevance, gp_params, n_folds = 10):
    """ Performs k-fold cross-validation.

    # Arguments:

    - dataset: the dataset as datasets.Dataset instance.

    - relevance: for retrieval tasks, an array specifying whether a sample
                 is relevant. Class relevance is given as 1, -1, or 0 if it
                 is not certain whether the label belongs to the class or not.
                 None for regression tasks.
    
    - gp_params: dictionary with keyword arguments passed to the GaussianProcess constructor.
    
    - n_folds: number of folds.

    # Returns:
        mean average precision for retrieval tasks or mean squared error for regression tasks.
    """
    
    not_unnameable = np.arange(len(dataset.X_train))
    if relevance is not None:
        relevance = np.asarray(relevance)
        not_unnameable = not_unnameable[relevance != 0]
    
    scores = np.ndarray((len(not_unnameable),), dtype = float)
    gp = GaussianProcess(dataset.X_train_norm, **gp_params)
    
    kfold = StratifiedKFold(n_folds, shuffle = True, random_state = 0) if relevance is not None else KFold(n_folds, shuffle = True, random_state = 0)
    for train_ind, test_ind in kfold.split(dataset.X_train_norm[not_unnameable], relevance[not_unnameable] if relevance is not None else None):
        gp.fit(not_unnameable[train_ind], relevance[not_unnameable[train_ind]] if relevance is not None else dataset.y_train[train_ind])
        scores[test_ind] = gp.predict_stored(not_unnameable[test_ind])
    
    return average_precision_score(relevance[not_unnameable], scores) if relevance is not None else -math.sqrt(mean_squared_error(dataset.y_train, scores))


def cross_validate_fewshot(dataset, relevance, gp_params, n_folds = 10):
    """ Performs k-fold cross-validation, but training on the smaller fraction of the data and evaluating on the larger one.

    # Arguments:

    - dataset: the dataset as datasets.Dataset instance.

    - relevance: for retrieval tasks, an array specifying whether a sample
                 is relevant. Class relevance is given as 1, -1, or 0 if it
                 is not certain whether the label belongs to the class or not.
                 None for regression tasks.
    
    - gp_params: dictionary with keyword arguments passed to the GaussianProcess constructor.
    
    - n_folds: number of folds.

    # Returns:
        mean average precision for retrieval tasks or mean squared error for regression tasks over all splits.
    """
    
    not_unnameable = np.arange(len(dataset.X_train))
    if relevance is not None:
        relevance = np.asarray(relevance)
        not_unnameable = not_unnameable[relevance != 0]
    
    gp = GaussianProcess(dataset.X_train_norm, **gp_params)
    perf = []
    
    kfold = StratifiedKFold(n_folds, shuffle = True, random_state = 0) if relevance is not None else KFold(n_folds, shuffle = True, random_state = 0)
    for train_ind, test_ind in kfold.split(dataset.X_train_norm[not_unnameable], relevance[not_unnameable] if relevance is not None else None):
        gp.fit(not_unnameable[test_ind], relevance[not_unnameable[test_ind]] if relevance is not None else dataset.y_train[test_ind])
        scores = gp.predict_stored(not_unnameable[train_ind])
        perf.append(average_precision_score(relevance[not_unnameable[train_ind]], scores) if relevance is not None else -math.sqrt(mean_squared_error(dataset.y_train[train_ind], scores)))
    
    return np.mean(perf)


def optimize_gp_params(dataset, relevance, grid = default_grids['full'], init = default_init, n_folds = 10, fewshot = False, verbose = 1):
    """ Optimizes the hyper-parameters of a GP kernel for a certain dataset.

    # Arguments:

    - dataset: the dataset as datasets.Dataset instance.

    - relevance: for retrieval tasks, an array specifying whether a sample
                 is relevant. Class relevance is given as 1, -1, or 0 if it
                 is not certain whether the label belongs to the class or not.
                 None for regression tasks.
    
    - grid: dictionary mapping hyper-parameter names to lists of values to be tried.

    - init: dictionary mapping hyper-parameter names to initial values.

    - n_folds: number of folds for k-fold cross-validation.

    - fewshot: boolean specifying whether the GP should be trained on the smaller fraction
               of the data and evaluated on the bigger one instead of the normal
               k-fold cross-validation.
    
    - verbose: verbosity level between 0 and 2.

    # Returns:
        - dictionary mapping parameter names to the best values found
        - performance measure obtained with those parameters
    """
    
    param_names = list(grid.keys())
    cur_params = [init[p] for p in param_names]
    changed = [True] * len(param_names)
    changing_param = 0
    perf = {}
    best_perf = -np.infty
    
    data_norm = np.sum(dataset.X_train_norm ** 2, axis = -1)
    pdist = ne.evaluate('A + B - 2 * C', { 'A' : data_norm[:,None], 'B' : data_norm[None,:], 'C' : np.dot(dataset.X_train_norm, dataset.X_train_norm.T) })
    
    while any(changed):
        
        cur_perfs = {}
        for val in grid[param_names[changing_param]]:
            cv_params ={ param_names[i] : val if i == changing_param else cur_params[i] for i in range(len(param_names)) }
            cv_params['pdist'] = pdist
            if fewshot:
                cur_perfs[val] = cross_validate_fewshot(dataset, relevance, cv_params, n_folds = n_folds)
            else:
                cur_perfs[val] = cross_validate_gp(dataset, relevance, cv_params, n_folds = n_folds)
            if verbose > 1:
                print('    {} = {} : {:.4f}'.format(param_names[changing_param], val, cur_perfs[val]))
        best_val = max(cur_perfs.keys(), key = lambda v: cur_perfs[v])
        
        if cur_perfs[best_val] < best_perf:
            break
        best_perf = cur_perfs[best_val]
        
        if verbose > 0:
            print('{} : {:.4f}'.format(', '.join('{} = {}'.format(param_names[i], best_val if i == changing_param else cur_params[i]) for i in range(len(param_names))), best_perf))
        
        changed[changing_param] = (best_val != cur_params[changing_param])
        cur_params[changing_param] = best_val
        perf[tuple(cur_params)] = best_perf
        changing_param = (changing_param + 1) % len(param_names)
        
        if len(param_names) < 2:
            break
    
    best_params = max(perf.keys(), key = lambda p: perf[p])
    return dict(zip(param_names, best_params)), best_perf if relevance is not None else -best_perf



if __name__ == '__main__':
    
    # Parse arguments
    config_file = None
    overrides = {}
    for arg in sys.argv[1:]:
        if arg.lower() == '--help':
            config_file = None
            break
        elif arg.startswith('--'):
            k, v = arg[2:].split('=', maxsplit = 1)
            overrides[k] = v
        elif config_file is None:
            config_file = arg
        else:
            print('Unexpected argument: {}'.format(arg))
            exit()
    if config_file is None:
        print()
        print('Optimizes GP hyper-parameters for a given dataset using alternating optimization.')
        print()
        print('Usage: {} <experiment-config-file> [--<override-option>=<override-value> ...]'.format(sys.argv[0]))
        print()
        print('The [EXPERIMENT] section of the given config file may specify the following')
        print('configuration directives to control the optimization:')
        print()
        print('     - grid: either "full" to optimize length scale, variance, and noise of the')
        print('             kernel or "ls_only" to optimize the length scale only (default: full).')
        print('     - n_folds: number of folds for k-fold cross validation (default: 10).')
        print('     - few_shot: boolean specifying whether the GP should be trained on the')
        print('                 smaller fraction of the data and evaluated on the bigger')
        print('                 one instead of the normal k-fold cross-validation (default: False).')
        print('     - verbosity: verbosity level between 0 and 2 (default: 1).')
        print()
        print('All directives from the [EXPERIMENT] section may also be overriden on the')
        print('command line by passing --key=value arguments.')
        print()
        exit()
    
    # Load dataset
    config, dataset = utils.load_dataset_from_config(config_file, 'EXPERIMENT', overrides)
    is_regression = isinstance(dataset, RegressionDataset)
    if is_regression:
    
        best_params, best_perf = optimize_gp_params(dataset, None, default_grids[config.get('EXPERIMENT', 'grid', fallback = 'full')],
                                                    n_folds = config.getint('EXPERIMENT', 'n_folds', fallback = 10),
                                                    fewshot = config.getboolean('EXPERIMENT', 'few_shot', fallback = False),
                                                    verbose = config.getint('EXPERIMENT', 'verbosity', fallback = 1))
        
        print('Best parameters for regression (RMSE: {:.2f}): {!r}'.format(best_perf, best_params))
    
    else:
        
        query_classes = str(config.get('EXPERIMENT', 'query_classes', fallback = '')).split()
        if len(query_classes) == 0:
            query_classes = list(dataset.class_relevance.keys())
        else:
            for i in range(len(query_classes)):
                try:
                    query_classes[i] = int(query_classes[i])
                except ValueError:
                    pass
    
        # Optimize GP parameters individually for each class
        best_params = {}
        best_perf = {}
        datasets = dataset.datasets() if isinstance(dataset, MultitaskRetrievalDataset) else [dataset]
        for di, dataset in enumerate(datasets):
            for lbl in query_classes:
                print('--- DATASET {}, CLASS {} ---'.format(di + 1, lbl))
                relevance, _ = dataset.class_relevance[lbl]
                lbl_best, lbl_perf = optimize_gp_params(dataset, relevance, default_grids[config.get('EXPERIMENT', 'grid', fallback = 'full')],
                                                        n_folds = config.getint('EXPERIMENT', 'n_folds', fallback = 10),
                                                        fewshot = config.getboolean('EXPERIMENT', 'few_shot', fallback = False),
                                                        verbose = config.getint('EXPERIMENT', 'verbosity', fallback = 1))
                best_params[(di,lbl)] = lbl_best
                best_perf[(di,lbl)] = lbl_perf
                print()

        # Print results
        for di, lbl in best_params.keys():
            print('Best parameters for dataset {}, class {} (AP: {:.2f}): {!r}'.format(di + 1, lbl, best_perf[(di,lbl)], best_params[(di,lbl)]))
