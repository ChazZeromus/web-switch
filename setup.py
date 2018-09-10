import os
from setuptools import setup, find_packages

name = 'webswitch'
description = 'Channel-style websocket server'
version = '0.0.1'
depends=[
	'websockets>=6.0',
	'dataclasses>=0.6',
]
entry_points={
	'console_scripts': [
		'webswitch-serve = webswitch.channel_server:cli_main'
	],
}

setup(
	name=name,
	version=version,
	description=description,
	author='Chaz Zeromus',
	author_email='chaz.zeromus@gmail.com',
	packages=find_packages('.', exclude=['tests']),
	install_requires=depends,
	entry_points=entry_points,
)
