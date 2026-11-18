# -*- coding: utf-8 -*-
"""Internationalisation QGIS/Qt : chaînes françaises source, catalogues .qm optionnels."""
from pathlib import Path
from qgis.PyQt.QtCore import QCoreApplication, QLocale, QTranslator, QSettings

CONTEXT = 'CorrectionGeometrique'
_translators = []  # garder les références Python pendant la vie du plugin


def tr(source):
    """Traduire une chaîne de l'interface, sans toucher aux identifiants métiers."""
    return QCoreApplication.translate(CONTEXT, source)


def install_translator():
    """Installer le fichier i18n/CorrectionGeometrique_<langue>.qm correspondant à QGIS.

    Respecte /locale/userLocale défini par QGIS puis la locale système Qt.
    Retourne None si aucun catalogue compilé n'est disponible.
    """
    locale = str(QSettings().value('locale/userLocale', '', type=str)).replace('-', '_')
    if not locale:
        locale = QLocale.system().name()
    dossier = Path(__file__).resolve().parent / 'i18n'
    candidats = [locale]
    langue = locale.split('_')[0]
    if langue not in candidats:
        candidats.append(langue)
    for code in candidats:
        catalogue = dossier / ('CorrectionGeometrique_' + code + '.qm')
        if catalogue.is_file():
            traducteur = QTranslator()
            if traducteur.load(str(catalogue)):
                QCoreApplication.installTranslator(traducteur)
                _translators.append(traducteur)
                return traducteur
    return None
