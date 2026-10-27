from setuptools import Distribution, setup

class DataDistribution(Distribution):
    has_data = Distribution.has_data_files

setup(distclass=DataDistribution)
