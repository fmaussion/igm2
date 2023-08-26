#!/usr/bin/env python3

# Copyright (C) 2021-2023 Guillaume Jouvet <guillaume.jouvet@unil.ch>
# Published under the GNU GPL (Version 3), check at the LICENSE file 
 
import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf
import igm

# Collect defaults, overide from json file, and parse all core parameters 
parser = igm.params_core()
igm.overide_from_json_file(parser,check_if_params_exist=False)
params = parser.parse_args(args=[])

# get the list of all modules in order
modules = params.modules_preproc + params.modules_process + params.modules_postproc

# add custom modules from file (must be called my_module_name.py) to igm
for module in modules:
    igm.load_custom_module(params, module)

# Collect defaults, overide from json file, and parse all specific module parameters 
for module in modules:
    getattr(igm, "params_" + module)(parser)
igm.overide_from_json_file(parser,check_if_params_exist=True)
params = parser.parse_args(args=[])

# print definive parameters in a file for record
if params.print_params:
    igm.print_params(params)
 
# Define a state class/dictionnary that contains all the data
state = igm.State()

# if logging is activated, add a logger to the state
if params.log:
    igm.add_logger(params, state) 

# Place the computation on your device GPU ('/GPU:0') or CPU ('/CPU:0')
with tf.device("/GPU:0"):

    # Initialize all the model components in turn
    for module in modules:
        getattr(igm, "init_" + module)(params, state)

    # Time loop, perform the simulation until reaching the defined end time
    if hasattr(state, "t"):
        while state.t < params.tend:
            # Update each model components in turn
            for module in modules:
                getattr(igm, "update_" + module)(params, state)
            
    # Finalize each module in turn
    for module in modules:
        getattr(igm, "final_" + module)(params, state)