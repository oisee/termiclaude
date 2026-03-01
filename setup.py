from setuptools import setup

setup(
    name='termiclaude',
    version='0.1.0',
    description='Autonomous supervisor for interactive CLI agents',
    py_modules=['termiclaude'],
    python_requires='>=3.10',
    extras_require={
        'anthropic': ['anthropic'],
        'openai': ['openai'],
    },
    entry_points={
        'console_scripts': [
            'termiclaude=termiclaude:main',
        ],
    },
)
