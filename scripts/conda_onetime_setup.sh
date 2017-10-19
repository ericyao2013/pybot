#!/bin/bash
conda config --add channels menpo
conda create --name pybot --file conda_requirements.txt -y

# Activate pybot and add activate/deactiavte scripts
source activate pybot
mkdir -p $CONDA_PREFIX/etc/conda/activate.d
mkdir -p $CONDA_PREFIX/etc/conda/deactivate.d

cp config/activate $CONDA_PREFIX/etc/conda/activate.d/env_vars.sh
cp config/deactivate $CONDA_PREFIX/etc/conda/deactivate.d/env_vars.sh

