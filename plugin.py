# -*- coding: utf-8 -*-
"""Point d'entrée QGIS, barre d'outils et exécution du traitement."""
from pathlib import Path

from .version import versioned_title
from .i18n import tr, install_translator

from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:  # QGIS 3 / Qt5
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import QMessageBox


class CorrectionGeometriquePlugin:
    def __init__(self, iface):
        self.iface = iface
        self._translator = install_translator()
        self.toolbar = None
        self.action = None
        self._running = False

    def initGui(self):
        self.toolbar = self.iface.addToolBar(tr('Correction géométrique'))
        self.toolbar.setObjectName('CorrectionGeometriqueToolbar')
        # Icône fournie avec le plugin, indépendante du thème QGIS.
        icon = QIcon(str(Path(__file__).with_name('icon.svg')))
        self.action = QAction(icon, tr('Corriger les géométries des polygones'), self.iface.mainWindow())
        self.action.setObjectName('CorrectionGeometriqueAction')
        self.action.setToolTip(tr('Contrôler et corriger les superpositions et doublons de la couche polygonale active'))
        self.action.triggered.connect(self.run)
        self.toolbar.addAction(self.action)
        self.iface.addPluginToVectorMenu(tr('Correction géométrique'), self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginVectorMenu(tr('Correction géométrique'), self.action)
            if self.toolbar is not None:
                self.toolbar.removeAction(self.action)
            self.action.deleteLater()
            self.action = None
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self._translator is not None:
            from qgis.PyQt.QtCore import QCoreApplication
            QCoreApplication.removeTranslator(self._translator)
            self._translator = None

    def run(self):
        if self._running:
            return
        self._running = True
        if self.action is not None:
            self.action.setEnabled(False)
        try:
            source = Path(__file__).with_name('traitement.py')
            context = {
                '__name__': 'Correction_Geometrique.traitement',
                '__file__': str(source),
                '__package__': 'Correction_Geometrique',
                'iface': self.iface,
            }
            exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), context)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self.iface.mainWindow(),
                                 versioned_title(tr('Correction géométrique — erreur')),
                                 tr('{}\n\nConsultez la console Python QGIS pour le détail.').format(exc))
        finally:
            self._running = False
            if self.action is not None:
                self.action.setEnabled(True)
