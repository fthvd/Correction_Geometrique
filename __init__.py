# -*- coding: utf-8 -*-
"""Extension Correction géométrique, QGIS 3 et 4."""

def classFactory(iface):
    from .plugin import CorrectionGeometriquePlugin
    return CorrectionGeometriquePlugin(iface)
