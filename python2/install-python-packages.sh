#! /bin/bash
eval "$(pyenv init -)"
pyenv shell 2.7.11
pip install pyzmq namedlist
pip install numpy
pip install matplotlib
pip install scipy
pip install pandas
pyenv rehash
