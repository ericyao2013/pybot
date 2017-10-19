#!/bin/bash
conda config --add channels menpo
conda create --name pybot --file conda_requirements.txt -y

# Activate pybot and add activate/deactiavte scripts
source activate pybot
mkdir -p $CONDA_PREFIX/etc/conda
cp config/activate $CONDA_PREFIX/etc/conda/
cp config/deactivate $CONDA_PREFIX/etc/conda/

