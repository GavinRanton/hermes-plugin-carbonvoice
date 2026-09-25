"""Packaging entry point; runtime registration lives in registration.py.

Setuptools executes setup.py inside an isolated PEP 517 build environment.
Importing the Hermes gateway here would require runtime dependencies at
metadata-generation time and prevent managed plugin installation.
"""

from setuptools import setup

setup()
