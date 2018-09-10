import os
from setuptools import setup

# This is a stub-only package as detailed in PEP-0561

name = 'webswitch'
package = 'webswitch'
description = 'Channel-style websocket server'
version = '0.2'
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
	packages=[package],
	install_requires=depends,
	entry_points=entry_points,
)
