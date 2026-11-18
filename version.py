# -*- coding: utf-8 -*-
"""Version affichée, lue dans les métadonnées officielles du plugin."""
from configparser import ConfigParser
from pathlib import Path


def plugin_version():
    metadata = ConfigParser(interpolation=None)
    with Path(__file__).with_name('metadata.txt').open(encoding='utf-8') as stream:
        metadata.read_file(stream)
    return metadata.get('general', 'version')


PLUGIN_VERSION = plugin_version()


def versioned_title(title):
    return '{} — version {}'.format(title, PLUGIN_VERSION)
