from setuptools import setup, find_packages

setup(
    name="max-requests",
    packages=find_packages('src') + ['twisted.plugins'],
    install_requires=[
        "attr",
        "Twisted",
    ],
    package_dir={'': 'src'},
)
