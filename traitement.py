# -*- coding: utf-8 -*-
"""QGIS 3.44 / PyQGIS : correction des recouvrements par valeur de partition.

La source reste inchangée. Sortie : GeoPackage avec les champs d'origine +
'correction' (oui / non / ambigu_FID) et 'fid_source'.

Avec 'partition' : traitements UNIQUEMENT entre valeurs identiques et non vides.
Les chevauchements partiels sont regroupés en une seule fenêtre finale,
avec sélection et zoom sur la carte, cases à cocher et choix automatique du masque (plus petit polygone).
Les inclusions multiples (jointives, disjointes ou chevauchantes) sont réunies par lots puis soustraites au contenant, sans limite fixe au nombre de polygones inclus.
La fenêtre groupée ne concerne que les chevauchements partiels.
Les intersections partielles sont traitees couple par couple, sans utiliser la geometrie totale du polygone voisin comme masque. Aucun chevauchement partiel n’est découpé automatiquement, quelle que soit sa surface : chaque découpe nécessite seulement une ligne cochée dans la fenêtre commune.
Les ambiguïtés ne déclenchent jamais la suppression intégrale des entités.
Une ligne cochée retire l’intersection du plus grand polygone ; le plus petit sert de masque.
Une couche corrigée provisoire est affichée AVANT cette fenêtre,
pour examiner sur la carte les inclusions déjà découpées.
Sans 'partition' : inclusions complètes corrigées ; recouvrements partiels
non corrigés et signalés 'ambigu'.
"""
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path
import re
import math
import os
import csv
import tempfile
from decimal import Decimal

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsGeometryParameters, QgsWkbTypes,
    QgsSpatialIndex, QgsField, QgsVectorFileWriter, QgsFillSymbol, QgsVariantUtils,
    QgsCategorizedSymbolRenderer, QgsRendererCategory
)
from qgis.gui import QgsHighlight
from qgis.PyQt.QtCore import Qt, QCoreApplication, QSettings, QMetaType, QEventLoop
try:
    from qgis.PyQt.QtCore import QVariant
except ImportError:  # Qt6 : QVariant peut ne plus être exposé
    QVariant = None
from qgis.core import Qgis
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QProgressDialog, QApplication, QDialog, QVBoxLayout, QFormLayout,
    QComboBox, QDialogButtonBox, QLineEdit, QPushButton, QHBoxLayout, QCheckBox,
    QFileDialog, QMessageBox, QLabel, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QHeaderView
)
import time

from .version import PLUGIN_VERSION, versioned_title
from .i18n import tr
from . import corrections_script as cs

# Les versions QGIS 3.40+ et 4.x acceptent les types QMetaType pour QgsField.
# Utiliser les enums de Qt plutôt qu'un test du numéro QGIS : leur présence
# est le critère pertinent et évite de mélanger QVariant.Type et QMetaType.Type.
# La branche QVariant est conservée seulement comme repli défensif.
if hasattr(QMetaType, 'Type') and hasattr(QMetaType.Type, 'QString'):
    CHAMP_ENTIER = QMetaType.Type.LongLong
    CHAMP_TEXTE = QMetaType.Type.QString
    CHAMP_REEL = QMetaType.Type.Double
elif QVariant is not None:
    CHAMP_ENTIER = QVariant.LongLong
    CHAMP_TEXTE = QVariant.String
    CHAMP_REEL = QVariant.Double
else:
    raise RuntimeError('Types de champs Qt non pris en charge (QMetaType/QVariant).')

TOL_AIRE = 0.0001  # m2 (SCR projeté en mètres)
COUCHE = iface.activeLayer()
if not isinstance(COUCHE, QgsVectorLayer) or not COUCHE.isValid():
    raise RuntimeError(tr('Sélectionnez la couche polygonale SOURCE.'))
if COUCHE.geometryType() != QgsWkbTypes.PolygonGeometry:
    raise RuntimeError(tr('La couche doit être de type polygone.'))
if COUCHE.crs().isGeographic() or not COUCHE.crs().isValid():
    raise RuntimeError(tr('Utilisez un SCR projeté valide (par ex. EPSG:2154).'))
if QgsWkbTypes.hasZ(COUCHE.wkbType()) or QgsWkbTypes.hasM(COUCHE.wkbType()):
    raise RuntimeError(tr('Les géométries Z/M ne sont pas gérées sans perte.'))
if QgsWkbTypes.isCurvedType(COUCHE.wkbType()):
    raise RuntimeError(tr('Les géométries courbes ne sont pas gérées.'))
champs = COUCHE.fields().names()
if any(c.casefold() == 'correction' for c in champs):
    raise RuntimeError(tr('Sélectionnez la couche source sans champ correction.'))
if any(c.casefold() == 'fid_source' for c in champs):
    raise RuntimeError(tr('La couche source contient déjà fid_source : ne pas écraser cette donnée.'))
# paramètres persistants, réglables avant chaque traitement.
# Le champ choisi remplace le champ fixe "partition".
REG = QSettings()
PREFIXE = 'correction_trous_polygones/v14/'  # Clé historique conservée pour préserver les préférences
SENTINELLE = tr('(Aucun champ — inclusions seules)')
CHAMPS_CANDIDATS = [n for n in champs if n.casefold() not in (
    'fid', 'fid_source', 'correction')]

# Nom de fichier de contrôle : département explicitement identifié à partir
# du nom de la source (ex. ZONE_URBA_004) ou d'un code INSEE homogène.
def departement_couche():
    noms = (Path(COUCHE.source().split('|')[0]).stem, COUCHE.name())
    for nom in noms:
        match = re.search(r'(?:^|[_\- ])(0?(?:0[1-9]|[1-8][0-9]|9[0-5])|97[1-6]|98[4-9])(?:$|[_\- .])', nom)
        if match:
            return match.group(1).zfill(3)
    # Valeurs de partition typiques DU_04123 ou codes INSEE 04123.
    for champ in champs:
        if champ.casefold() not in ('partition', 'insee', 'code_insee', 'insee_com', 'codecommune'):
            continue
        codes = set()
        for entite in COUCHE.getFeatures():
            value = str(entite[champ])
            match = re.search(r'(?:^|_)(\d{2})\d{3}(?:$|_)', value)
            if match:
                codes.add(match.group(1).zfill(3))
                if len(codes) > 1:
                    break
        if len(codes) == 1:
            return codes.pop()
    return None


def nom_sortie_corrigee(nom_source, dossier, extension):
    """Nom court et numéro croissant, commun au fichier et à la couche."""
    nom_source = re.sub(r'\.(?:gpkg|shp|geojson)$', '', nom_source.strip(), flags=re.IGNORECASE)
    base = re.sub(r'[^\w-]+', '_', nom_source, flags=re.UNICODE).strip('_')
    numero_source = 0
    # Reconnaître les noms historiques ainsi que les sorties script/plugin.
    motif_date = r'_(?:PARTITIONS_CORRIGEES|repare_sens_horaire|controle_sens(?:_repare)?|controle)_\d{8}_\d{6}(?:_\d{6})?$'
    motif_numero = r'[_-]+corr(?:e)?ction[_-]+(?:(?:plugin|script)[_-]+)?([1-9][0-9]*)$'
    while True:
        ancien = base
        base = re.sub(motif_date, '', base, flags=re.IGNORECASE).rstrip('_')
        base = re.sub(r'_+PARTITIONS_CORRIGEES$', '', base, flags=re.IGNORECASE).rstrip('_')
        match = re.search(motif_numero, base, flags=re.IGNORECASE)
        if match:
            numero_source = max(numero_source, int(match.group(1)))
            base = base[:match.start()].rstrip('_')
        if base == ancien:
            break
    base = base or 'polygones'
    # Marge pour le suffixe, les composants SHP et les rapports annexes.
    longueur = lambda texte: len(str(texte).encode('utf-16-le')) // 2
    budget = min(80, 230 - longueur(Path(dossier) / '') - 45)
    if budget < 8:
        raise RuntimeError(tr('Dossier de sortie trop long : {}').format(dossier))
    while longueur(base) > budget:
        base = base[:-1]
    base = base.rstrip('_')
    dossier = Path(dossier)
    expression = re.compile(re.escape(base) + r'[_ -]+corr(?:e)?ction[_ -]+plugin[_ -]+([1-9][0-9]*)(?:[_.]|$)', re.IGNORECASE)
    numeros = [numero_source]
    if dossier.is_dir():
        for fichier in dossier.iterdir():
            match = expression.match(fichier.name)
            if match:
                numeros.append(int(match.group(1)))
    numero = max(numeros) + 1
    nom = '{}_Correction plugin {}'.format(base, numero)
    return nom, dossier / (nom + extension)


def detecter_doublons_geometriques(geometries, attributs, champs_compares,
                                 champ_partition=None, suivi=None):
    """Égalité GEOS exacte des surfaces, indépendante de leur encodage WKB.

    Aucun tampon, arrondi ou seuil de surface pour assimiler deux objets.
    Une divergence attributaire reste soumise au choix de l'utilisateur.
    """
    index_groupes = QgsSpatialIndex()
    groupes, representants, partitions = {}, {}, {}
    for numero, fid in enumerate(sorted(geometries), 1):
        if suivi is not None:
            suivi(numero, len(geometries), fid)
        geom = geometries[fid]
        partition = repr(attributs[fid][champ_partition]) if champ_partition else None
        reference = None
        for ref in sorted(index_groupes.intersects(geom.boundingBox())):
            if partitions[ref] == partition and geom.isGeosEqual(representants[ref]):
                reference = ref
                break
        if reference is None:
            reference = fid
            representants[fid] = geom
            partitions[fid] = partition
            groupes[fid] = []
            objet = QgsFeature()
            objet.setId(fid)
            objet.setGeometry(geom)
            index_groupes.addFeature(objet)
        groupes[reference].append(fid)
    supprimes, divergents = {}, []
    for membres in groupes.values():
        conserves = []
        for fid in membres:
            identique = next((ref for ref in conserves
                if all(attributs[fid][nom] == attributs[ref][nom]
                       for nom in champs_compares)), None)
            if identique is None:
                conserves.append(fid)
            else:
                supprimes[fid] = identique
        if len(conserves) > 1:
            divergents.append(conserves)
    return supprimes, divergents


# Précision des seules opérations d'overlay : 1 micromètre dans le SCR métrique.
# Cette grille reste très inférieure à la précision de la donnée et stabilise
# le nœudage des bandes quasi colinéaires ; aucun buffer n'est appliqué.
GRILLE_CALCUL = 1e-6


def operation_geos_robuste(a, b, operation, aire_seule=False):
    parametres = QgsGeometryParameters()
    parametres.setGridSize(GRILLE_CALCUL)
    resultat = getattr(a, operation)(b, parametres)
    if resultat.isNull():
        raise ValueError('Opération GEOS impossible : ' + operation)
    return resultat.area() if aire_seule else resultat


def tolerance_precision(geom):
    return max(TOL_AIRE, geom.length() * GRILLE_CALCUL * 2)


def stabiliser_corrections(geometries, attributs, champs_compares, champ_partition,
                          decisions, choix_decoupes=None, choix_doublons=None,
                          suivi=None, tolerance=0.0001, limite=100, automatique=False, nettoyer=None, inclusions=None):
    """Réexaminer les résultats courants jusqu'au point fixe, sans réorientation.

    Les décisions None sont des refus explicites, toujours respectés.
    L'index initial reste valable : les géométries ne font que rétrécir.
    """
    courantes = {fid: QgsGeometry(g) for fid, g in geometries.items()}
    index = QgsSpatialIndex()
    for fid, geom in courantes.items():
        f = QgsFeature()
        f.setId(fid)
        f.setGeometry(geom)
        index.addFeature(f)
    doubles, choisis, incidents_stables = {}, {}, []
    a_revoir = set(courantes)
    for cycle in range(1, limite + 1):
        if suivi:
            suivi(cycle, 0, len(a_revoir))
        if inclusions is not None:
            inclusions_cycle = inclusions(courantes)
            for fid in inclusions_cycle:
                courantes.pop(fid, None)
            a_revoir.update(inclusions_cycle.values())
        exacts, divergents = detecter_doublons_geometriques(
            courantes, attributs, champs_compares, champ_partition)
        selection = choix_doublons(divergents) if divergents and choix_doublons else {}
        exclus = dict(exacts)
        exclus.update(selection)
        if exclus:
            # Un doublon exact peut référencer un représentant écarté par choix.
            for fid, ref in list(exclus.items()):
                while ref in exclus:
                    ref = exclus[ref]
                exclus[fid] = ref
            doubles.update({fid: exclus[fid] for fid in exacts})
            choisis.update({fid: exclus[fid] for fid in selection})
            for fid in exclus:
                courantes.pop(fid, None)
            a_revoir.update(exclus.values())
        candidats, masques, vus = [], defaultdict(list), set()
        # Les conflits sont inventoriés sur un instantané cohérent de ce cycle.
        for numero, fid in enumerate(sorted(a_revoir), 1):
            if fid not in courantes:
                continue
            if suivi and (numero == 1 or numero % 25 == 0):
                suivi(cycle, numero, len(a_revoir))
            ga = courantes[fid]
            for oid in sorted(index.intersects(ga.boundingBox())):
                paire = tuple(sorted((fid, oid)))
                if fid == oid or oid not in courantes or paire in vus:
                    continue
                vus.add(paire)
                if not automatique and paire in decisions and decisions[paire] is None:
                    continue
                if champ_partition:
                    va, vb = attributs[fid][champ_partition], attributs[oid][champ_partition]
                    if (va is None or QgsVariantUtils.isNull(va) or not str(va).strip()
                            or va != vb or str(va) == 'NULL'):
                        continue
                gb = courantes[oid]
                try:
                    if ga.isGeosEqual(gb):
                        # Attributs divergents conservés : jamais vider un doublon.
                        continue
                    intersection = operation_geos_robuste(ga, gb, 'intersection')
                    if intersection.isNull() or intersection.area() <= tolerance:
                        continue
                    marge = max(tolerance, min(ga.area(), gb.area()) * 1e-9)
                    a_dans_b = operation_geos_robuste(ga, gb, 'difference', True) <= marge
                    b_dans_a = operation_geos_robuste(gb, ga, 'difference', True) <= marge
                    if a_dans_b != b_dans_a:
                        cible = oid if a_dans_b else fid
                        masques[cible].append(gb if cible == fid else ga)
                    elif champ_partition:
                        if not automatique and paire in decisions:
                            cible = decisions[paire]
                            if cible is not None:
                                masques[cible].append(gb if cible == fid else ga)
                        else:
                            candidats.append(dict(a=fid, b=oid,
                                partition=str(attributs[fid][champ_partition]),
                                commune=texte_commune(attributs[fid]),
                                aire=intersection.area(), geom=intersection))
                except Exception as exc:
                    incidents_stables.append('Cycle {} couple {} : {}'.format(cycle, paire, exc))
        if candidats:
            choix = choix_decoupes(candidats, courantes) if choix_decoupes else [None] * len(candidats)
            if len(choix) != len(candidats):
                raise RuntimeError(tr('Nombre de décisions de découpe incohérent'))
            for ligne, cible in zip(candidats, choix):
                paire = tuple(sorted((ligne['a'], ligne['b'])))
                if cible is not None and cible not in paire:
                    raise RuntimeError(tr('Cible de découpe étrangère au couple'))
                decisions[paire] = cible
                if cible is not None:
                    support = ligne['b'] if cible == ligne['a'] else ligne['a']
                    masques[cible].append(courantes[support])
        changes = set()
        for fid, morceaux in sorted(masques.items()):
            try:
                parametres_union = QgsGeometryParameters()
                parametres_union.setGridSize(GRILLE_CALCUL)
                masque = QgsGeometry.unaryUnion(morceaux, parametres_union) if len(morceaux) > 1 else morceaux[0]
                avant = courantes[fid]
                aire = operation_geos_robuste(avant, masque, 'intersection', True)
                if aire <= tolerance:
                    continue
                apres = multipolygone_sur(operation_geos_robuste(avant, masque, 'difference'))
                if nettoyer is not None:
                    apres = nettoyer(fid, apres)
                if apres.isNull() or apres.isEmpty() or not apres.isGeosValid():
                    raise ValueError('Découpe vide ou invalide ; objet conservé')
                marge = max(tolerance, aire * 1e-9,
                            (avant.length() + masque.length()) * GRILLE_CALCUL * 2)
                if (operation_geos_robuste(apres, masque, 'intersection', True) > marge
                        or abs(avant.area() - apres.area() - aire) > marge
                        or operation_geos_robuste(apres, avant, 'difference', True) > marge):
                    raise ValueError('Bilan surfacique incohérent : retiré={}, attendu={}, résidu={}, ajout={}, tol={}'.format(avant.area()-apres.area(), aire, operation_geos_robuste(apres, masque, 'intersection', True), operation_geos_robuste(apres, avant, 'difference', True), marge))
                if not apres.isGeosEqual(avant):
                    courantes[fid] = apres
                    changes.add(fid)
            except Exception as exc:
                incidents_stables.append('Cycle {} FID {} : {}'.format(cycle, fid, exc))
        if not changes:
            # Une passe partielle ne prouve pas la stabilité de la commune.
            # Repartir de TOUS les survivants avant d'autoriser la livraison.
            if a_revoir != set(courantes):
                a_revoir = set(courantes)
                continue
            return courantes, doubles, choisis, incidents_stables, cycle
        # Réexaminer aussi tous les voisins : la cible et le sens de priorité
        # peuvent changer après le retrait d'une intersection ou d'un doublon.
        a_revoir = set(changes)
        for fid in changes:
            a_revoir.update(oid for oid in index.intersects(courantes[fid].boundingBox())
                           if oid in courantes)
    raise RuntimeError(tr('Stabilisation non obtenue après {} cycles internes : '
                       'aucune livraison finale validée.').format(limite))



def nom_defaut_controle(extension):
    departement = departement_couche()
    if departement is None:
        raise RuntimeError(
            tr('Numéro de département introuvable dans la source. '
            'Choisissez un nom de fichier avec un suffixe départemental sur 3 chiffres.'))
    nom_couche = re.sub(r'[^\w-]+', '_', COUCHE.name(), flags=re.UNICODE).strip('_')
    # Retirer les suffixes départementaux déjà présents avant d'en ajouter un seul.
    # Accepte 04 et 004, ainsi qu'un ancien nom doublé 004_004.
    suffixes = {departement, departement.lstrip('0') or '0'}
    if departement.isdigit():
        suffixes.add(departement.zfill(2))
        suffixes.add(str(int(departement)).zfill(2))
    motif = r'(?:^|[_-])(?:' + '|'.join(re.escape(code) for code in sorted(suffixes)) + r')$'
    while re.search(motif, nom_couche):
        nom_couche = re.sub(motif, '', nom_couche).rstrip('_-')
    parties = ['CORRECTIONS_GEOMETRIQUES']
    if nom_couche:
        parties.append(nom_couche)
    parties.append(departement)
    return '_'.join(parties) + extension

class ParametresTraitement(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(versioned_title(tr('Correction des polygones — paramètres')))
        self.setMinimumWidth(590)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            tr('Les entités seront regroupées par valeur identique du champ choisi.\n'
            'Sans champ, les inclusions sont corrigées ; les chevauchements partiels restent ambigus.')))
        self.examen = QCheckBox(tr('Examiner les corrections géométriques avant leur application (choix manuel)'))
        self.examen.setToolTip(
            tr('Coché : afficher la fenêtre de choix avec cases à cocher, tri et zoom.\n'
            'Décoché : appliquer automatiquement les découpes partielles valides,\n'
            'avec le plus petit polygone comme masque du plus grand.'))
        self.examen.setChecked(REG.value(PREFIXE + 'examen_avant_correction', True,
                                          type=bool))
        layout.addWidget(self.examen)
        formulaire = QFormLayout()
        self.champ = QComboBox()
        self.champ.addItem(SENTINELLE, None)
        for nom_champ in CHAMPS_CANDIDATS:
            self.champ.addItem(nom_champ, nom_champ)
        memorise = REG.value(PREFIXE + 'champ', '', type=str)
        candidat = next((n for n in CHAMPS_CANDIDATS if n == memorise), None)
        if candidat is None and memorise == '':
            candidat = next((n for n in CHAMPS_CANDIDATS if n.casefold() == 'partition'), None)
        self.champ.setCurrentIndex(max(0, self.champ.findData(candidat)))
        formulaire.addRow(tr('Colonne de regroupement :'), self.champ)

        self.mode = QComboBox()
        self.mode.addItem(tr('Couche mémoire temporaire'), 'memory')
        self.mode.addItem(tr('Fichier physique'), 'file')
        self.mode.setCurrentIndex(max(0, self.mode.findData(
            REG.value(PREFIXE + 'mode', 'memory', type=str))))
        formulaire.addRow(tr('Sortie des emprises des découpes :'), self.mode)

        self.format = QComboBox()
        self.format.addItem(tr('GeoPackage (*.gpkg)'), ('GPKG', '.gpkg'))
        self.format.addItem(tr('GeoJSON (*.geojson)'), ('GeoJSON', '.geojson'))
        self.format.addItem(tr('Shapefile (*.shp)'), ('ESRI Shapefile', '.shp'))
        self.format.setCurrentIndex(max(0, self.format.findText(
            REG.value(PREFIXE + 'format', 'GeoPackage (*.gpkg)', type=str))))
        formulaire.addRow(tr('Format du fichier :'), self.format)

        ligne = QHBoxLayout()
        self.chemin = QLineEdit()
        ancien_chemin = REG.value(PREFIXE + 'chemin', '', type=str)
        repertoire = Path(ancien_chemin).parent if ancien_chemin else Path(COUCHE.source().split('|')[0]).parent
        self.chemin.setText(str(repertoire / nom_defaut_controle(self.format.currentData()[1])))
        self.parcourir = QPushButton(tr('Parcourir…'))
        ligne.addWidget(self.chemin)
        ligne.addWidget(self.parcourir)
        formulaire.addRow(tr('Chemin du fichier :'), ligne)
        layout.addLayout(formulaire)
        layout.addWidget(QLabel(
            tr('En sortie fichier, les deux couches utilisent le même format et le même dossier.\n'
            'En mode mémoire du contrôle, la couche corrigée reste en GeoPackage près de la source.')))
        boutons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(boutons)
        boutons.accepted.connect(self.valider)
        boutons.rejected.connect(self.reject)
        self.mode.currentIndexChanged.connect(self.maj_etat)
        self.format.currentIndexChanged.connect(self.changer_format)
        self.parcourir.clicked.connect(self.choisir_fichier)
        self.maj_etat()

    def maj_etat(self):
        fichier = self.mode.currentData() == 'file'
        self.format.setEnabled(fichier)
        self.chemin.setEnabled(fichier)
        self.parcourir.setEnabled(fichier)

    def changer_format(self):
        texte = self.chemin.text().strip()
        if texte:
            self.chemin.setText(str(Path(texte).with_suffix(self.format.currentData()[1])))

    def choisir_fichier(self):
        extension = self.format.currentData()[1]
        defaut = self.chemin.text().strip() or str(
            Path(COUCHE.source().split('|')[0]).parent / nom_defaut_controle(extension))
        fichier, _ = QFileDialog.getSaveFileName(
            self, tr('Enregistrer les surfaces retirées'), defaut,
            tr('Fichiers {} (*{})').format(extension.upper(), extension))
        if fichier:
            self.chemin.setText(str(Path(fichier).with_suffix(extension)))

    def valider(self):
        if self.mode.currentData() == 'file':
            chemin = self.chemin.text().strip()
            if not chemin:
                QMessageBox.warning(self, tr('Chemin requis'),
                                    tr('Choisissez le chemin du fichier de contrôle.'))
                return
            fichier = Path(chemin).with_suffix(self.format.currentData()[1])
            if not fichier.parent.is_dir():
                QMessageBox.warning(self, tr('Dossier absent'),
                                    tr('Le dossier de destination n’existe pas.'))
                return
            if fichier.exists():
                reponse = QMessageBox.question(
                    self, tr('Remplacer le fichier de contrôle ?'),
                    tr('Le fichier CORRECTIONS_GEOMETRIQUES existe déjà :\n\n{}\n\n'
                    'Voulez-vous le remplacer ? Cette opération écrasera le fichier existant.').format(fichier),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if reponse != QMessageBox.StandardButton.Yes:
                    return  # Laisser le dialogue ouvert pour changer de chemin ou annuler.
            self.chemin.setText(str(fichier))
        REG.setValue(PREFIXE + 'examen_avant_correction', self.examen.isChecked())
        REG.setValue(PREFIXE + 'champ', self.champ.currentData() or '')
        REG.setValue(PREFIXE + 'mode', self.mode.currentData())
        REG.setValue(PREFIXE + 'format', self.format.currentText())
        REG.setValue(PREFIXE + 'chemin', self.chemin.text().strip())
        self.accept()

fenetre_parametres = ParametresTraitement(iface.mainWindow())
if fenetre_parametres.exec() != QDialog.DialogCode.Accepted:
    raise RuntimeError(tr('Traitement annulé avant toute modification.'))
partition_champ = fenetre_parametres.champ.currentData()
mode_retraits = fenetre_parametres.mode.currentData()
format_retraits = fenetre_parametres.format.currentData()
chemin_retraits = fenetre_parametres.chemin.text().strip()
examen_avant_correction = fenetre_parametres.examen.isChecked()

source = list(COUCHE.getFeatures())
if not source:
    raise RuntimeError(tr('Couche vide.'))

# Fenêtre de suivi : la commune est déduite des champs usuels, sinon du nom
# de la couche. Aucun champ attributaire n'est modifié pour l'affichage.
COMMUNE_CANDIDATS = (
    'nom_commune', 'nom_com', 'commune', 'libcom', 'nomcommune',
    'insee_com', 'insee', 'code_insee', 'codecommune', 'idcom'
)
commune_champ = next((c for nom in COMMUNE_CANDIDATS for c in champs
                      if c.casefold() == nom), None)

def texte_commune(feat):
    if commune_champ:
        valeur = feat[commune_champ]
        if valeur is not None and str(valeur).strip() not in ('', 'NULL'):
            return str(valeur)
    return COUCHE.name()

class AnnulationUtilisateur(Exception):
    pass

progression = QProgressDialog(tr('Préparation...'), tr('Annuler'), 0, 100, iface.mainWindow())
progression.setWindowTitle(versioned_title(tr('Correction géométrique — progression')))
progression.setWindowModality(Qt.WindowModality.WindowModal)
progression.setMinimumDuration(0)
progression.setAutoClose(False)
progression.setAutoReset(False)
progression.setMinimumWidth(500)
progression.setValue(0)
progression.show()
_dernier_refresh = 0.0

def avancer(phase, indice, total, debut, fin, feat=None):
    # Plusieurs phases cumulées : l'indicateur reste croissant.
    global _dernier_refresh
    proportion = min(1.0, float(indice) / max(1, total))
    pourcentage = int(debut + (fin - debut) * proportion)
    progression.setValue(max(progression.value(), pourcentage))
    if feat is not None:
        commune = texte_commune(feat)
        label = ('{}\nCommune : {}\nPolygone FID : {}  ({}/{})\n{} %'
                 .format(phase, commune, feat.id(), indice, total, pourcentage))
    else:
        label = '{}\n{}/{}  —  {} %'.format(phase, indice, total, pourcentage)
    progression.setLabelText(label)
    now = time.monotonic()
    if now - _dernier_refresh >= 0.06 or indice == total:
        QApplication.processEvents()
        _dernier_refresh = now
    if progression.wasCanceled():
        raise AnnulationUtilisateur(tr('Traitement annulé : aucune couche source modifiée.'))

# Fermer la fenêtre à la fin, même en cas de message d'erreur.
# La fermeture normale est exécutée après les contrôles de l'export.

# Etat d'affichage à restaurer si l'utilisateur annule ou si une erreur survient.
_noeud_source = QgsProject.instance().layerTreeRoot().findLayer(COUCHE.id())
_source_visible_initialement = (_noeud_source.itemVisibilityChecked()
                                if _noeud_source is not None else True)
_apercu_id = None
_traitement_termine = False
_dossier_travail = None

def multipolygone_sur(geom):
    """Extraire récursivement les surfaces, sans réparer ni perdre de surface.

    GEOS peut retourner une collection valide contenant des lignes résiduelles.
    convertToMultiType() seul ne retire pas ces composantes non surfaciques.
    """
    if geom.isNull() or geom.isEmpty():
        raise ValueError('Géométrie polygonale vide')
    parties = []

    def extraire(g):
        t = QgsWkbTypes.flatType(g.wkbType())
        if t == QgsWkbTypes.Polygon:
            parties.append(QgsGeometry(g))
        elif t in (QgsWkbTypes.MultiPolygon, QgsWkbTypes.GeometryCollection):
            for enfant in g.asGeometryCollection():
                extraire(enfant)
        elif t not in (QgsWkbTypes.Point, QgsWkbTypes.MultiPoint,
                        QgsWkbTypes.LineString, QgsWkbTypes.MultiLineString):
            raise ValueError('Type géométrique non pris en charge : {}'.format(g.wkbType()))

    extraire(geom)
    if not parties:
        raise ValueError('Aucune surface polygonale dans le résultat')
    resultat = QgsGeometry.collectGeometry(parties)
    # Certaines versions renvoient Polygon pour une seule partie.
    if QgsWkbTypes.flatType(resultat.wkbType()) == QgsWkbTypes.Polygon:
        if not resultat.convertToMultiType():
            raise ValueError('Conversion du polygone unique en MultiPolygon impossible')
    if (QgsWkbTypes.flatType(resultat.wkbType()) != QgsWkbTypes.MultiPolygon or
            resultat.isEmpty() or not resultat.isGeosValid()):
        raise ValueError('Résultat MultiPolygon invalide')
    if abs(resultat.area() - geom.area()) > max(1e-8, abs(geom.area()) * 1e-12):
        raise ValueError('Surface modifiée pendant la normalisation polygonale')
    return resultat


def geometries_export_equivalentes(avant, apres):
    """Comparer les surfaces, indépendamment du regroupement des anneaux SHP.

    Ne tolérer aucun écart de surface : après remise en validité des seules
    représentations invalides, GEOS doit confirmer l'égalité topologique.
    """
    if avant.isNull() or apres.isNull() or avant.isEmpty() or apres.isEmpty():
        return False
    if avant.isGeosValid() and apres.isGeosValid():
        return avant.isGeosEqual(apres)
    try:
        a = avant if avant.isGeosValid() else multipolygone_sur(avant.makeValid())
        b = apres if apres.isGeosValid() else multipolygone_sur(apres.makeValid())
        return a.isGeosEqual(b)
    except Exception:
        return False








try:
    origine = {f.id(): f for f in source}
    geoms = {}
    invalides = set()
    reparations = {}
    echecs_reparation = {}
    idx = QgsSpatialIndex()
    script_avant = {}
    script_final = {}
    nettoyages_finaux = set()
    orientations_script = set()
    inclusions_script = {}
    seuil_script = cs.SEUIL_RESIDU_M2 if COUCHE.crs().mapUnits() == Qgis.DistanceUnit.Meters else 0.0
    for numero, f in enumerate(source, 1):
        avancer(tr('Réparation et nettoyage intégrés'), numero, len(source), 0, 15, f)
        original = f.geometry()
        g, etat_avant, reparation_script, detail_script = cs.reparer(original, seuil_script)
        script_avant[f.id()] = dict(geom_avant=etat_avant, reparation=reparation_script,
                                    rep_detail=detail_script, nettoyages=[])
        if g.isNull() or g.isEmpty() or not g.isGeosValid():
            invalides.add(f.id())
            echecs_reparation[f.id()] = detail_script or 'Géométrie vide ou invalide conservée'
            continue
        if reparation_script == 'ECHEC':
            # Un micro-objet isolé reste conservé ; ce refus ne bloque pas
            # les autres corrections et est inscrit dans le rapport.
            echecs_reparation[f.id()] = detail_script
        if bytes(g.asWkb()) != bytes(original.asWkb()):
            reparations[f.id()] = QgsGeometry(g)
        geoms[f.id()] = QgsGeometry(g)
    print('RÉPARATION : {} géométries réparées ; {} échecs conservés.'.format(
        len(reparations), len(echecs_reparation)))

    # Dédupliquer avant les découpes, indépendamment de l'ordre des
    # sommets, des anneaux et des parties relus par le fournisseur.
    champs_reglementaires = cs.noms_attributs_metier(champs)
    retraits_script = cs.trouver_inclusions(
        geoms, origine, champs_reglementaires,
        suivi=lambda n, total, fid: avancer(tr('Inclusions à attributs identiques'),
                                            n, total, 15, 15, origine[fid]))
    doublons_script = {}
    for fid, parent in retraits_script.items():
        if geoms[fid].isGeosEqual(geoms[parent]):
            doublons_script[fid] = parent
        else:
            inclusions_script[fid] = parent
    for fid in retraits_script:
        geoms.pop(fid, None)

    doublons_supprimes, doublons_douteux = detecter_doublons_geometriques(
        geoms, origine, champs_reglementaires, partition_champ,
        suivi=lambda n, total, fid: avancer(tr('Recherche des doublons géométriques'),
                                            n, total, 15, 15, origine[fid]))
    doublons_supprimes.update(doublons_script)
    # décision groupée des géométries identiques aux attributs divergents.
    # Présenter chaque attribut divergent dans la ligne, sans suppression implicite.
    doublons_choisis = {}  # fid écarté -> fid conservé

    class FenetreDoublonsDivergents(QDialog):
        def __init__(self, groupes, parent=None):
            super().__init__(parent)
            self.setWindowTitle(versioned_title(tr('Doublons géométriques — attributs divergents')))
            self.resize(1230, 580)
            self.groupes = groupes
            self.table = QTableWidget(len(groupes), 5, self)
            self.table.setHorizontalHeaderLabels([
                tr('Traiter'), tr('Partition'), tr('FID concernés'),
                tr('Attributs différents (valeurs par FID)'), tr('FID à conserver')])
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.verticalHeader().setVisible(False)
            self.table.setSortingEnabled(False)
            self.choix = {}
            for row, ids in enumerate(groupes):
                cellule = QTableWidgetItem()
                cellule.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                cellule.setCheckState(Qt.CheckState.Unchecked)
                cellule.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, 0, cellule)
                val_part = origine[ids[0]][partition_champ] if partition_champ else 'SANS_PARTITION'
                divergents = [nom for nom in champs_reglementaires
                              if len({repr(origine[fid][nom]) for fid in ids}) > 1]
                details = ' | '.join(
                    '{} : {}'.format(nom, ' / '.join(
                        'FID {} = {}'.format(fid, str(origine[fid][nom]))
                        for fid in ids)) for nom in divergents)
                for col, valeur in ((1, str(val_part)),
                                    (2, ', '.join(str(fid) for fid in ids)),
                                    (3, details)):
                    cell = QTableWidgetItem(valeur)
                    cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    cell.setToolTip(valeur)
                    self.table.setItem(row, col, cell)
                select = QComboBox(self.table)
                select.addItem(tr('Conserver tous les objets'), None)
                for fid in ids:
                    select.addItem(tr('Conserver FID {}').format(fid), fid)
                self.table.setCellWidget(row, 4, select)
                self.choix[row] = select
            self.table.setSortingEnabled(True)
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(
                tr('Chaque ligne représente une même géométrie ayant des attributs différents.\n'
                'Les noms des champs divergents et leurs valeurs par FID sont affichés.\n'
                'Cochez une ligne puis choisissez le FID à conserver ; sinon tous sont conservés.')))
            layout.addWidget(self.table)
            outils = QHBoxLayout()
            b_tout = QPushButton(tr('Cocher tout'))
            b_rien = QPushButton(tr('Décocher tout'))
            b_tout.clicked.connect(lambda: self.cocher(True))
            b_rien.clicked.connect(lambda: self.cocher(False))
            outils.addWidget(b_tout)
            outils.addWidget(b_rien)
            outils.addStretch()
            layout.addLayout(outils)
            boutons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            boutons.accepted.connect(self.verifier)
            boutons.rejected.connect(self.reject)
            layout.addWidget(boutons)

        def cocher(self, etat):
            for row in range(self.table.rowCount()):
                self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if etat else Qt.CheckState.Unchecked)

        def verifier(self):
            for row in range(self.table.rowCount()):
                cell = self.table.item(row, 0)
                if cell.checkState() == Qt.CheckState.Checked:
                    idx = int(cell.data(Qt.ItemDataRole.UserRole))
                    if self.choix[idx].currentData() is None:
                        QMessageBox.warning(self, tr('FID à conserver'),
                            tr('Choisissez un FID à conserver pour la ligne cochée : {}').format(
                                ', '.join(str(x) for x in self.groupes[idx])))
                        return
            self.accept()

        def decisions(self):
            resultats = {}
            for row in range(self.table.rowCount()):
                cell = self.table.item(row, 0)
                if cell.checkState() != Qt.CheckState.Checked:
                    continue
                idx = int(cell.data(Qt.ItemDataRole.UserRole))
                garde = self.choix[idx].currentData()
                for fid in self.groupes[idx]:
                    if fid != garde:
                        resultats[fid] = garde
            return resultats

    if doublons_douteux:
        avancer(tr('Construction de la liste des doublons attributaires'), 0,
                len(doublons_douteux), 15, 15)
        fenetre_doublons = FenetreDoublonsDivergents(
            doublons_douteux, iface.mainWindow())
        avancer(tr('Liste des doublons attributaires prête'), 1, 1, 15, 15)
        progression.hide()
        try:
            if fenetre_doublons.exec() != QDialog.DialogCode.Accepted:
                raise AnnulationUtilisateur(tr('Choix des doublons annulé : aucune sortie produite.'))
            doublons_choisis = fenetre_doublons.decisions()
        finally:
            progression.show()
        print('DOUBLONS DIVERGENTS : {} exclusions choisies sur {} groupes'.format(
            len(doublons_choisis), len(doublons_douteux)))

    # Les doublons exclus par choix ne doivent pas servir de masques.
    for fid in doublons_choisis:
        geoms.pop(fid, None)
    for fid in doublons_supprimes:
        geoms.pop(fid, None)
    for fid in geoms:
        objet_index = QgsFeature(origine[fid])
        objet_index.setGeometry(geoms[fid])
        idx.addFeature(objet_index)
    print('DOUBLONS EXACTS : {} entites redondantes exclues des calculs ; '
          '{} groupes avec attributs divergents conserves.'.format(
              len(doublons_supprimes), len(doublons_douteux)))
    for fid, ref in sorted(doublons_supprimes.items()):
        print('DOUBLON EXACT partition={} fid_supprime={} fid_conserve={}'.format(
            origine[fid][partition_champ] if partition_champ else 'SANS_PARTITION', fid, ref))

    # Index des groupes. Les valeurs nulles et vides ne sont jamais assimilées
    # à une partition valide (elles requièrent une décision métier).
    groupes = defaultdict(list)
    manquants = set()
    for fid in geoms:
        if partition_champ is None:
            continue
        valeur = origine[fid][partition_champ]
        if valeur is None or str(valeur).strip() == '' or str(valeur) == 'NULL':
            manquants.add(fid)
        else:
            groupes[(type(valeur).__name__, str(valeur))].append(fid)

    statuts = {f.id(): 'non' for f in source}
    for fid in invalides:
        statuts[fid] = 'ambigu'
    modifs = {fid: QgsGeometry(g) for fid, g in reparations.items()}
    for fid in reparations:
        statuts[fid] = 'oui'
    # Identifiants des polygones responsables d'une ambiguïté.
    # Conserver les FID QGIS des entités SOURCE, pas ceux du GPKG de sortie.
    causes_ambigues = defaultdict(set)

    def marquer_ambigu(fid, autre_fid=None):
        if autre_fid is not None and autre_fid != fid:
            causes_ambigues[fid].add(int(autre_fid))
        if statuts[fid] != 'oui':
            statuts[fid] = 'ambigu'

    incidents = ['Réparation impossible FID {} : {}'.format(fid, motif)
                 for fid, motif in sorted(echecs_reparation.items())]
    paires = 0

    if partition_champ is None:
        # Sans champ partition : CORRIGER les inclusions complètes ;
        # conserver les chevauchements PARTIELS en ambiguïté.
        # La décision est prise sur les géométries ORIGINALES, avant découpe.
        enfants_par_parent = defaultdict(set)
        vus = set()
        tol_relative = 1e-9

        for numero, (fid, g) in enumerate(geoms.items(), 1):
            avancer(tr("Recherche des inclusions / recouvrements"), numero, len(geoms), 15, 65, origine[fid])
            for oid in idx.intersects(g.boundingBox()):
                if oid == fid or oid not in geoms:
                    continue
                paire = tuple(sorted((fid, oid)))
                if paire in vus:
                    continue
                vus.add(paire)
                autre = geoms[oid]
                try:
                    aire_commune = operation_geos_robuste(g, autre, 'intersection').area()
                    if aire_commune <= TOL_AIRE:
                        continue
                    aire_g, aire_o = g.area(), autre.area()
                    marge = max(TOL_AIRE, min(aire_g, aire_o) * tol_relative)
                    g_dans_o = (aire_g < aire_o - marge and
                                aire_g - aire_commune <= marge and
                                autre.contains(g))
                    o_dans_g = (aire_o < aire_g - marge and
                                aire_o - aire_commune <= marge and
                                g.contains(autre))
                    if g_dans_o:
                        enfants_par_parent[oid].add(fid)
                    elif o_dans_g:
                        enfants_par_parent[fid].add(oid)
                    else:
                        # Simple chevauchement partiel : aucune soustraction.
                        # 'oui' reste prioritaire en cas de correction distincte.
                        marquer_ambigu(fid, oid)
                        marquer_ambigu(oid, fid)
                        paires += 1
                except Exception as exc:
                    marquer_ambigu(fid, oid)
                    marquer_ambigu(oid, fid)
                    incidents.append('Analyse {} / {} : {}'.format(fid, oid, exc))

        # Découper uniquement les contenants, en conservant tous les originaux.
        # Une correction réussie est 'oui' même si d'autres recouvrements partiels
        # ont été signalés pour la même entité.
        for numero, (parent, enfants) in enumerate(sorted(enfants_par_parent.items()), 1):
            avancer(tr("Découpe des inclusions"), numero, len(enfants_par_parent), 65, 85, origine[parent])
            avant = geoms[parent]
            resultat = QgsGeometry(avant)
            succes = []
            for enfant in sorted(enfants):
                soustrait = geoms[enfant]
                try:
                    aire_commune_avant = operation_geos_robuste(resultat, soustrait, 'intersection').area()
                    if aire_commune_avant <= TOL_AIRE:
                        # Déjà soustrait par une opération antérieure.
                        continue
                    essai = multipolygone_sur(operation_geos_robuste(resultat, soustrait, 'difference'))
                    if (essai.isNull() or essai.isEmpty() or
                            not essai.isGeosValid()):
                        raise ValueError('Différence vide ou invalide')
                    aire_residuelle = operation_geos_robuste(essai, soustrait, 'intersection').area()
                    marge = max(tolerance_precision(resultat), aire_commune_avant * 1e-9)
                    retiree = resultat.area() - essai.area()
                    if aire_residuelle > marge or abs(retiree - aire_commune_avant) > marge:
                        raise ValueError('Découpe non validée : surface résiduelle ou retirée incohérente')
                    resultat = essai
                    succes.append(enfant)
                except Exception as exc:
                    incidents.append('Découpe parent {} / enfant {} : {}'.format(parent, enfant, exc))
                    marquer_ambigu(parent, enfant)
            if succes:
                modifs[parent] = QgsGeometry(resultat)
                statuts[parent] = 'oui'
                paires += len(succes)
                print('CORRIGE parent={} enfants={}'.format(parent, succes))

    else:
        # Signaler les géométries dépourvues de valeur de partition impliquées
        # dans un chevauchement, sans les corriger.
        for numero, fid in enumerate(sorted(manquants), 1):
            avancer(tr("Contrôle des partitions manquantes"), numero, len(manquants), 15, 25, origine[fid])
            g = geoms[fid]
            for oid in idx.intersects(g.boundingBox()):
                if oid == fid or oid not in geoms:
                    continue
                try:
                    if operation_geos_robuste(g, geoms[oid], 'intersection').area() > TOL_AIRE:
                        marquer_ambigu(fid, oid)
                        break
                except Exception:
                    marquer_ambigu(fid, oid)
                    break

        # inventaire, inclusions automatiques, puis décisions groupées.
        # Les inclusions complètes de même partition sont découpées automatiquement
        # dans le contenant, et protégées contre l’exclusion des ambiguïtés.
        proteges_inclusion = set()
        enfants_par_parent_partition = defaultdict(set)
        traites_partition = 0
        total_partition = sum(len(ids) for ids in groupes.values())
        chevauchements = []
        for cle, ids in groupes.items():
            ids = sorted(ids)
            for indice, fid in enumerate(ids):
                traites_partition += 1
                avancer(tr('Inventaire des chevauchements : {}').format(cle[1]),
                        traites_partition, total_partition, 25, 82, origine[fid])
                for oid in ids[indice + 1:]:
                    ga, gb = geoms[fid], geoms[oid]
                    if not ga.boundingBox().intersects(gb.boundingBox()):
                        continue
                    try:
                        intersection = operation_geos_robuste(ga, gb, 'intersection')
                        aire_orig = intersection.area()
                        if aire_orig <= TOL_AIRE:
                            continue
                        marge = max(TOL_AIRE, min(ga.area(), gb.area()) * 1e-9)
                        inclusion = (operation_geos_robuste(ga, gb, 'difference').area() <= marge or
                                     operation_geos_robuste(gb, ga, 'difference').area() <= marge)
                        if inclusion:
                            proteges_inclusion.update((fid, oid))
                            # Définir explicitement le polygone contenu et le contenant.
                            # Utiliser les surfaces de DIFFERENCE et non l'ordre des FID.
                            # Les géométries identiques sont ambiguës : aucun trou possible.
                            a_dans_b = operation_geos_robuste(ga, gb, 'difference').area() <= marge
                            b_dans_a = operation_geos_robuste(gb, ga, 'difference').area() <= marge
                            if a_dans_b and not b_dans_a:
                                enfants_par_parent_partition[oid].add(fid)
                            elif b_dans_a and not a_dans_b:
                                enfants_par_parent_partition[fid].add(oid)
                            else:
                                marquer_ambigu(fid, oid)
                                marquer_ambigu(oid, fid)
                            continue
                        candidat = dict(
                            a=fid, b=oid, partition=cle[1], aire=aire_orig,
                            commune=texte_commune(origine[fid]),
                            geom=QgsGeometry(intersection))
                        chevauchements.append(candidat)
                    except Exception as exc:
                        incidents.append('Analyse couple {} / {} : {}'.format(fid, oid, exc))
                        marquer_ambigu(fid, oid)
                        marquer_ambigu(oid, fid)

        # un contenant peut avoir un nombre quelconque d'enfants,
        # jointifs, disjoints ou se chevauchant entre eux. Les enfants ne sont
        # jamais découpes ici. La reunion est faite par groupes pour ne pas
        # construire une enorme operation GEOS en une seule fois.
        # Le masque est la reunion exacte des emprises d'origine de TOUS les
        # enfants de la partition ; chaque portion est comptee une seule fois.
        def reunion_par_lots(geometries, taille_lot=50):
            niveau = [QgsGeometry(g) for g in geometries]
            if not niveau:
                raise ValueError('Aucun enfant a unir')
            while len(niveau) > 1:
                suivant = []
                for debut in range(0, len(niveau), taille_lot):
                    paquet = niveau[debut:debut + taille_lot]
                    if len(paquet) == 1:
                        unite = paquet[0]
                    else:
                        parametres_union = QgsGeometryParameters()
                        parametres_union.setGridSize(GRILLE_CALCUL)
                        unite = QgsGeometry.unaryUnion(paquet, parametres_union)
                    if unite.isNull() or unite.isEmpty() or not unite.isGeosValid():
                        raise ValueError('Union GEOS invalide (lot {})'.format(debut // taille_lot + 1))
                    suivant.append(unite)
                niveau = suivant
            return niveau[0]

        for numero, (parent, enfants) in enumerate(
                sorted(enfants_par_parent_partition.items()), 1):
            enfants_valides = sorted(enfants)
            avancer(tr('Union des {} polygones inclus').format(len(enfants_valides)),
                    numero, len(enfants_par_parent_partition), 82, 84,
                    origine[parent])
            try:
                # Prendre l'original, pas un enfant deja corrige ni un masque
                # modifie par un autre conflit. Un parent peut contenir des
                # enfants emboites a plusieurs niveaux.
                avant = QgsGeometry(modifs.get(parent, geoms[parent]))
                masque_union = reunion_par_lots(
                    [geoms[e] for e in enfants_valides])
                surface_attendue = operation_geos_robuste(avant, masque_union, 'intersection').area()
                if surface_attendue <= TOL_AIRE:
                    # Une intersection deja retiree ne doit pas etre reclassee
                    # en anomalie ni comptee une deuxieme fois.
                    continue
                resultat = multipolygone_sur(operation_geos_robuste(avant, masque_union, 'difference'))
                if (resultat.isNull() or resultat.isEmpty() or
                        not resultat.isGeosValid()):
                    raise ValueError('Resultat vide ou invalide')
                retiree = avant.area() - resultat.area()
                residuelle = operation_geos_robuste(resultat, masque_union, 'intersection').area()
                marge = max(tolerance_precision(avant), surface_attendue * 1e-9)
                if (abs(retiree - surface_attendue) > marge or
                        residuelle > marge):
                    raise ValueError('Bilan incorrect: attendu={}, retire={}, residu={}'.format(
                        surface_attendue, retiree, residuelle))
                modifs[parent] = QgsGeometry(resultat)
                statuts[parent] = 'oui'
                paires += len(enfants_valides)
                print('INCLUSIONS MULTIPLES CORRIGEES partition={} parent={} '
                      'nombre_enfants={} enfants={} surface_unique_m2={:.6f}'.format(
                          origine[parent][partition_champ], parent,
                          len(enfants_valides), enfants_valides, retiree))
            except Exception as exc:
                incidents.append('Inclusions multiples parent {} enfants {} : {}'.format(
                    parent, enfants_valides, exc))
                for enfant in enfants_valides:
                    marquer_ambigu(parent, enfant)

        class ElementNombre(QTableWidgetItem):
            """Tri numerique des FID et surfaces, affichage inchange."""
            def __init__(self, texte, nombre):
                super().__init__(texte)
                self.nombre = float(nombre)

            def __lt__(self, autre):
                if isinstance(autre, ElementNombre):
                    return self.nombre < autre.nombre
                return super().__lt__(autre)

        class FenetreChoixDecoupes(QDialog):
            def __init__(self, lignes, parent=None, retour_progression=None):
                super().__init__(parent)
                self.setWindowTitle(versioned_title(tr('Choix groupé des découpes géométriques')))
                # Fenêtre indépendante et NON MODALE : les panneaux des couches,
                # la carte et les outils d'identification de QGIS restent actifs.
                # Le dialogue est conservé en mémoire par la procédure appelante
                # jusqu'à sa validation ou son annulation.
                self.setWindowFlags(
                    Qt.WindowType.Window
                    | Qt.WindowType.WindowCloseButtonHint
                    | Qt.WindowType.WindowMinMaxButtonsHint)
                self.setModal(False)
                self.setWindowModality(Qt.WindowModality.NonModal)
                self.resize(1030, 610)
                self.lignes = lignes
                self.surlignage = None
                self.table = QTableWidget(len(lignes), 7, self)
                self.table.setHorizontalHeaderLabels([
                    tr('Traiter'), tr('Commune'), tr('Partition'), tr('FID A'), tr('FID B'),
                    tr('Surface commune (m²)'), tr('Masque / Découpé')])
                self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
                self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
                self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
                self.table.verticalHeader().setVisible(False)
                self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
                self.table.horizontalHeader().setStretchLastSection(True)
                # Ne jamais activer le tri avant la fin du remplissage du tableau.
                self.table.setSortingEnabled(False)
                for r, item in enumerate(lignes):
                    if retour_progression is not None and (r == 0 or r % 25 == 0 or r == len(lignes) - 1):
                        retour_progression(r + 1, len(lignes))
                    case = QTableWidgetItem()
                    case.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                    case.setCheckState(Qt.CheckState.Unchecked)
                    # Index original, independant de la position apres tri.
                    case.setData(Qt.ItemDataRole.UserRole, r)
                    self.table.setItem(r, 0, case)
                    valeurs = (
                        QTableWidgetItem(str(item['commune'])),
                        QTableWidgetItem(str(item['partition'])),
                        ElementNombre(str(item['a']), item['a']),
                        ElementNombre(str(item['b']), item['b']),
                        ElementNombre('{:.3f}'.format(item['aire']), item['aire']),
                    )
                    for col, cellule in enumerate(valeurs, 1):
                        cellule.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                        self.table.setItem(r, col, cellule)
                    masque = self.fid_masque(item)
                    grand = item['b'] if masque == item['a'] else item['a']
                    info = QTableWidgetItem('Masque {} → découpe {}'.format(masque, grand))
                    info.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.table.setItem(r, 6, info)
                self.table.setSortingEnabled(True)
                self.table.horizontalHeader().setSortIndicatorShown(True)
                self.table.itemSelectionChanged.connect(self.zoomer)
                layout = QVBoxLayout(self)
                layout.addWidget(QLabel(
                    tr('Cliquez sur un en-tête pour trier les lignes (second clic : ordre inverse).\n'
                    'Cochez les chevauchements à traiter : le plus petit polygone sert de masque.\n'
                    'Un clic sur une ligne zoome sur le recouvrement dans la carte.\n'
                    'Les inclusions complètes sont déjà visibles dans la couche APERCU_CORRIGE.\n'
                    'Les découpes partielles choisies seront appliquées ensuite.\n'
                    'Vous pouvez interroger la carte et modifier la visibilité des couches QGIS '
                    'sans fermer cette fenêtre.')))
                layout.addWidget(self.table)
                outils = QHBoxLayout()
                tout = QPushButton(tr('Cocher tout'))
                rien = QPushButton(tr('Décocher tout'))
                tout.clicked.connect(lambda: self.cocher(True))
                rien.clicked.connect(lambda: self.cocher(False))
                outils.addWidget(tout)
                outils.addWidget(rien)
                outils.addStretch()
                layout.addLayout(outils)
                self.infos = QLabel(tr('Aucune découpe validée par défaut.'))
                layout.addWidget(self.infos)
                boutons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                boutons.accepted.connect(self.verifier)
                boutons.rejected.connect(self.reject)
                layout.addWidget(boutons)

            @staticmethod
            def fid_masque(item):
                """Choix stable, fondé sur les surfaces totales d'origine."""
                a, b = item['a'], item['b']
                aa, ab = geoms[a].area(), geoms[b].area()
                return a if (aa, a) <= (ab, b) else b

            def effacer_surlignage(self):
                """Retire QgsHighlight (QGraphicsItem, non QWidget) du canevas."""
                surlignage = self.surlignage
                self.surlignage = None
                if surlignage is None:
                    return
                try:
                    iface.mapCanvas().scene().removeItem(surlignage)
                finally:
                    # Pas de setParent() ni de deleteLater() : QgsHighlight
                    # herite de QGraphicsItem, pas de QObject/QWidget.
                    del surlignage

            def closeEvent(self, evenement):
                self.effacer_surlignage()
                super().closeEvent(evenement)

            def done(self, code):
                self.effacer_surlignage()
                super().done(code)

            def index_source(self, ligne_visible):
                cellule = self.table.item(ligne_visible, 0)
                return int(cellule.data(Qt.ItemDataRole.UserRole))

            def cocher(self, etat):
                for r in range(self.table.rowCount()):
                    self.table.item(r, 0).setCheckState(Qt.CheckState.Checked if etat else Qt.CheckState.Unchecked)

            def zoomer(self):
                self.effacer_surlignage()
                r = self.table.currentRow()
                if r < 0:
                    return
                item = self.lignes[self.index_source(r)]
                masque = self.fid_masque(item)
                grand = item['b'] if masque == item['a'] else item['a']
                # QgsHighlight est dessiné dans le canevas, au-dessus des couches,
                # même lorsque la couche source est masquée par APERCU_CORRIGE.
                self.surlignage = QgsHighlight(
                    iface.mapCanvas(), QgsGeometry(geoms[masque]), COUCHE)
                self.surlignage.setColor(QColor(255, 90, 0, 255))
                self.surlignage.setFillColor(QColor(255, 196, 0, 85))
                self.surlignage.setWidth(3)
                self.surlignage.show()
                rect = item['geom'].boundingBox()
                if not rect.isEmpty():
                    rect.scale(1.4)
                    iface.mapCanvas().setExtent(rect)
                iface.mapCanvas().refresh()
                self.infos.setText(
                    tr('Masque FID {} (plus petit) → découpe FID {} ; '
                    'intersection {:.3f} m²').format(masque, grand, item['aire']))

            def verifier(self):
                self.accept()

            def decisions(self):
                # Conserver l'identité de chaque couple malgré le tri visuel.
                resultat = [None] * len(self.lignes)
                for r in range(self.table.rowCount()):
                    i = self.index_source(r)
                    if self.table.item(r, 0).checkState() == Qt.CheckState.Checked:
                        masque = self.fid_masque(self.lignes[i])
                        ligne = self.lignes[i]
                        resultat[i] = (ligne['b'] if masque == ligne['a']
                                       else ligne['a'])
                return resultat

        # examen facultatif. Les deux modes partagent les mêmes tests de
        # validation, sans suppression de polygone entier.

        if chevauchements:
            # montrer les résultats des inclusions automatiques AVANT les
            # choix de découpes partielles. La couche d'aperçu ne contient que
            # un identifiant et les géométries, pour éviter les contraintes
            # attributaires rencontrées avec les couches mémoire précédentes.
            apercu = QgsVectorLayer(
                'MultiPolygon?crs={}'.format(COUCHE.crs().authid()),
                COUCHE.name() + '__APERCU_CORRIGE', 'memory')
            if not apercu.isValid():
                raise RuntimeError(tr('Impossible de créer la couche d’aperçu.'))
            fournisseur_apercu = apercu.dataProvider()
            fournisseur_apercu.addAttributes([QgsField('fid_source', CHAMP_ENTIER)])
            apercu.updateFields()
            # ecriture une entite a la fois afin d'isoler les refus du
            # fournisseur memoire. Les lots de 500 masquaient le FID fautif.
            nb_apercu = 0
            anomalies_apercu = []
            for numero_apercu, element in enumerate(source, 1):
                if numero_apercu == 1 or numero_apercu % 25 == 0 or numero_apercu == len(source):
                    avancer(tr('Création de l’aperçu cartographique corrigé'),
                            numero_apercu, len(source), 82, 83, element)
                fid = element.id()
                if fid in doublons_supprimes or fid in doublons_choisis or fid in inclusions_script:
                    continue
                original = QgsGeometry(element.geometry())
                # Les sources invalides sont conservées et signalées ambiguës.
                # La normalisation stricte ne concerne que les corrections.
                if fid in modifs:
                    try:
                        geom = multipolygone_sur(modifs[fid])
                    except ValueError as exc:
                        anomalies_apercu.append((fid, 'original affiché : {}'.format(exc)))
                        geom = QgsGeometry(original)
                else:
                    geom = QgsGeometry(original)
                if geom.isNull() or geom.isEmpty():
                    anomalies_apercu.append((fid, 'geometrie vide'))
                    continue
                if geom.type() != QgsWkbTypes.PolygonGeometry:
                    # Repli VISUEL seulement : les corrections calculees ne
                    # sont jamais alterees par ce controle d'affichage.
                    anomalies_apercu.append((fid, 'type non polygonal : original affiche'))
                    geom = QgsGeometry(original)
                if not geom.isMultipart():
                    geom.convertToMultiType()
                objet = QgsFeature(apercu.fields())
                objet.setGeometry(geom)
                objet.setAttributes([fid])
                ok, ajoutes = fournisseur_apercu.addFeatures([objet])
                if not ok or len(ajoutes) != 1:
                    # Nouvelle tentative avec la geometrie source uniquement
                    # si la geometrie corrigee posait probleme a l'affichage.
                    if not geom.equals(original):
                        secours = QgsGeometry(original)
                        if not secours.isMultipart():
                            secours.convertToMultiType()
                        original_objet = QgsFeature(apercu.fields())
                        original_objet.setGeometry(secours)
                        original_objet.setAttributes([fid])
                        ok2, ajoutes2 = fournisseur_apercu.addFeatures([original_objet])
                        if ok2 and len(ajoutes2) == 1:
                            nb_apercu += 1
                            anomalies_apercu.append((fid, 'original affiche en repli'))
                            continue
                    message = fournisseur_apercu.lastError() or 'erreur de fournisseur inconnue'
                    anomalies_apercu.append((fid, message))
                    continue
                nb_apercu += 1
            if anomalies_apercu:
                print('AVERTISSEMENT APERCU : {} entites a verifier'.format(
                    len(anomalies_apercu)))
                for fid_err, motif in anomalies_apercu[:30]:
                    print('  FID {} : {}'.format(fid_err, motif))
            # Une carte d'apercu lacunaire donnerait une information trompeuse.
            # La source reste disponible et le traitement s'arrete avec un
            # diagnostic exploitable si certaines entites sont manquantes.
            # les doublons exacts sont volontairement absents de
            # l'aperçu, car ils ne participeront pas à la couche corrigée.
            # Comparer au nombre ATTENDU après dédoublonnage, et non au total
            # d'entités de la couche source avant traitement.
            nb_attendu_apercu = len(source) - len(set(doublons_supprimes) | set(doublons_choisis) | set(inclusions_script))
            print('APERCU : {} entites affichees / {} attendues ; '
                  '{} doublons exacts exclus.'.format(
                      nb_apercu, nb_attendu_apercu, len(set(doublons_supprimes) | set(doublons_choisis) | set(inclusions_script))))
            if nb_apercu != nb_attendu_apercu:
                raise RuntimeError(
                    tr('Apercu incomplet : {}/{} entites attendues apres '
                    'dedoublonnage ({} dans la source, {} doublons exclus). '
                    'FID en erreur : {}. Voir le journal QGIS.').format(
                        nb_apercu, nb_attendu_apercu, len(source),
                        len(doublons_supprimes),
                        [fid_err for fid_err, _ in anomalies_apercu[:30]]))
            apercu.updateExtents()
            # Affecter un symbole simple, sans dépendance aux champs d'origine.
            from qgis.core import QgsSingleSymbolRenderer
            apercu.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
                'color': '87,188,151,105',
                'outline_color': '25,105,77,255',
                'outline_width': '0.35'
            })))
            QgsProject.instance().addMapLayer(apercu)
            _apercu_id = apercu.id()
            if _noeud_source is not None:
                _noeud_source.setItemVisibilityChecked(False)
            iface.mapCanvas().refresh()
            QApplication.processEvents()
            if examen_avant_correction:
                # La construction du tableau peut durer : ne pas masquer le suivi
                # avant que le tableau soit entièrement prêt.
                avancer(tr('Préparation de la fenêtre de choix des découpes'),
                        0, max(1, len(chevauchements)), 83, 84)
                dialogue = FenetreChoixDecoupes(
                    chevauchements, iface.mainWindow(),
                    retour_progression=lambda n, t: avancer(
                        tr('Construction de la fenêtre de choix : lignes'),
                        n, t, 83, 84))
                avancer(tr('Fenêtre de choix prête — ouverture'), 1, 1, 84, 84)
                progression.hide()
                try:
                    # show() laisse QGIS utilisable (carte, identification,
                    # couches). La boucle Qt locale garde cette fonction en
                    # attente de la décision sans lancer les découpes trop tôt.
                    # Elle traite TOUS les événements Qt, contrairement à un
                    # blocage Python, et ne rend pas le dialogue modal.
                    boucle_choix = QEventLoop()
                    dialogue.finished.connect(boucle_choix.quit)
                    dialogue.show()
                    dialogue.raise_()
                    dialogue.activateWindow()
                    # Si le dialogue a été fermé avant l'entrée dans la boucle,
                    # ne surtout pas attendre un signal déjà émis.
                    if dialogue.isVisible():
                        boucle_choix.exec()
                    if dialogue.result() != QDialog.DialogCode.Accepted:
                        raise AnnulationUtilisateur(tr('Choix annulé : aucune sortie corrigée produite.'))
                    choix_liste = dialogue.decisions()
                finally:
                    dialogue.close()
                    progression.show()
            else:
                # Même règle de masque que le mode manuel : la petite emprise
                # est conservée, la grande perd uniquement leur intersection.
                # Pas d'automatisme hors partition : dans ce cas, les recouvrements
                # partiels sont déclarés ambigus comme dans les versions précédentes.
                choix_liste = []
                for numero_auto, ligne in enumerate(chevauchements, 1):
                    a, b = ligne['a'], ligne['b']
                    if partition_champ:
                        cible = b if (geoms[a].area(), a) <= (geoms[b].area(), b) else a
                    else:
                        cible = None
                    choix_liste.append(cible)
                    if (numero_auto == 1 or numero_auto % 25 == 0 or
                            numero_auto == len(chevauchements)):
                        avancer(tr('Préparation des découpes automatiques'),
                                numero_auto, len(chevauchements), 83, 84, origine[a])
        else:
            choix_liste = []

        for num, (ligne, cible) in enumerate(zip(chevauchements, choix_liste), 1):
            fid, oid = ligne['a'], ligne['b']
            avancer(tr('Application des découpes géométriques validées'), num,
                    len(chevauchements), 82, 85, origine[fid])
            if cible is None:
                marquer_ambigu(fid, oid)
                marquer_ambigu(oid, fid)
                continue
            support_id = oid if cible == fid else fid
            try:
                avant_cible = QgsGeometry(modifs.get(cible, geoms[cible]))
                # Masque immuable : SEULE l'intersection originelle du couple.
                # La géométrie entière de l'autre polygone ne doit jamais
                # servir de masque (notamment en présence de trois zonages).
                masque_couple = QgsGeometry(ligne['geom'])
                aire = operation_geos_robuste(avant_cible, masque_couple, 'intersection').area()
                if aire <= TOL_AIRE:
                    continue
                essai = multipolygone_sur(operation_geos_robuste(avant_cible, masque_couple, 'difference'))
                if essai.isNull() or essai.isEmpty() or not essai.isGeosValid():
                    raise ValueError('Découpe vide ou invalide du FID {}'.format(cible))
                tol = max(tolerance_precision(avant_cible), aire * 1e-9)
                residu = operation_geos_robuste(essai, masque_couple, 'intersection').area()
                retiree = avant_cible.area() - essai.area()
                if residu > tol or abs(retiree - aire) > tol:
                    raise ValueError('Surface retirée incohérente FID {}'.format(cible))
                modifs[cible] = QgsGeometry(essai)
                statuts[cible] = 'oui'
                # Une ligne cochée validée clôt l'ambiguïté de CE couple,
                # même s'il avait été signalé pendant l'analyse préliminaire.
                causes_ambigues[fid].discard(oid)
                causes_ambigues[oid].discard(fid)
                paires += 1
                print('CONFIRME partition={} FID_a={} FID_b={} FID_decoupe={} surface_m2={:.3f}'.format(
                    ligne['partition'], fid, oid, cible, aire))
            except Exception as exc:
                incidents.append('Couple {} / {} partition {} : {}'.format(
                    fid, oid, ligne['partition'], exc))
                marquer_ambigu(fid, oid)
                marquer_ambigu(oid, fid)


    # Itérations internes sur les résultats actuels : aucune nouvelle exécution
    # du plugin n'est nécessaire pour les conflits devenus traitables.
    decisions_stables = {}
    if partition_champ:
        decisions_stables = {tuple(sorted((ligne['a'], ligne['b']))): cible
                            for ligne, cible in zip(chevauchements, choix_liste)}

    def choix_decoupes_stables(lignes, actuelles):
        if not examen_avant_correction:
            return [ligne['b'] if (actuelles[ligne['a']].area(), ligne['a']) <=
                    (actuelles[ligne['b']].area(), ligne['b']) else ligne['a']
                    for ligne in lignes]
        dialogue = FenetreChoixDecoupes(lignes, iface.mainWindow())
        progression.hide()
        try:
            if dialogue.exec() != QDialog.DialogCode.Accepted:
                raise AnnulationUtilisateur(tr('Choix des découpes supplémentaires annulé.'))
            return dialogue.decisions()
        finally:
            progression.show()

    groupes_deja_examines = {tuple(sorted(ids)) for ids in doublons_douteux}

    def choix_doublons_stables(groupes_nouveaux):
        nouveaux = [ids for ids in groupes_nouveaux
                    if tuple(sorted(ids)) not in groupes_deja_examines]
        if not nouveaux:
            return {}
        dialogue = FenetreDoublonsDivergents(nouveaux, iface.mainWindow())
        progression.hide()
        try:
            if dialogue.exec() != QDialog.DialogCode.Accepted:
                raise AnnulationUtilisateur(tr('Choix des nouveaux doublons annulé.'))
            resultat = dialogue.decisions()
            groupes_deja_examines.update(tuple(sorted(ids)) for ids in nouveaux)
            return resultat
        finally:
            progression.show()

    def nettoyer_resultat_script(fid, geom):
        resultat, etat, statut, detail = cs.reparer(geom, seuil_script)
        if statut in ('REPAREE', 'RESIDUS_SUPPRIMES'):
            nettoyages_finaux.add(fid)
            journal = script_avant[fid]['nettoyages']
            if detail and detail not in journal:
                journal.append(detail)
        elif statut in ('ECHEC', 'IMPOSSIBLE') and detail:
            journal = script_avant[fid]['nettoyages']
            if detail not in journal:
                journal.append(detail)
        return resultat

    def inclusions_resultats_script(courantes):
        retraits = cs.trouver_inclusions(courantes, origine, champs_reglementaires)
        retenus = {}
        for fid, parent in retraits.items():
            paire = tuple(sorted((fid, parent)))
            if examen_avant_correction and paire in decisions_stables and decisions_stables[paire] is None:
                continue
            retenus[fid] = parent
            if courantes[fid].isGeosEqual(courantes[parent]):
                doublons_supprimes[fid] = parent
            else:
                inclusions_script[fid] = parent
        return retenus

    actuelles = {fid: nettoyer_resultat_script(fid, QgsGeometry(modifs.get(fid, g)))
                 for fid, g in geoms.items()}
    stables, nouveaux_doubles, nouveaux_choisis, incidents_cycles, nb_cycles = stabiliser_corrections(
        actuelles, origine, champs_reglementaires, partition_champ, decisions_stables,
        choix_decoupes=choix_decoupes_stables if partition_champ else None,
        choix_doublons=choix_doublons_stables,
        suivi=lambda cycle, n, total: avancer(
            tr('Stabilisation interne : cycle {}').format(cycle), n, total, 85, 85),
        tolerance=TOL_AIRE, automatique=not examen_avant_correction, nettoyer=nettoyer_resultat_script, inclusions=inclusions_resultats_script)
    doublons_supprimes.update(nouveaux_doubles)
    doublons_choisis.update(nouveaux_choisis)
    incidents.extend(sorted(set(incidents_cycles)))
    # Réactualiser aussi les ambiguïtés : ne pas garder de références à
    # des doublons supprimés ou à des intersections déjà résolues.
    exclus_stables = set(doublons_supprimes) | set(doublons_choisis) | set(inclusions_script)
    for fid, autres in causes_ambigues.items():
        for oid in list(autres):
            if fid in exclus_stables or oid in exclus_stables:
                autres.discard(oid)
            elif fid in stables and oid in stables:
                intersection = operation_geos_robuste(stables[fid], stables[oid], 'intersection')
                if not intersection.isNull() and intersection.area() <= TOL_AIRE:
                    autres.discard(oid)
    for (fid, oid), cible in decisions_stables.items():
        if cible is None and fid in stables and oid in stables:
            intersection = operation_geos_robuste(stables[fid], stables[oid], 'intersection')
            if not intersection.isNull() and intersection.area() > TOL_AIRE:
                marquer_ambigu(fid, oid)
                marquer_ambigu(oid, fid)
    for fid, geom in stables.items():
        if not geom.isGeosEqual(actuelles[fid]):
            modifs[fid] = QgsGeometry(geom)
    print('STABILISATION : {} cycle(s) interne(s), {} doublon(s) supplémentaire(s).'.format(
        nb_cycles, len(nouveaux_doubles) + len(nouveaux_choisis)))

    exclus_script = set(doublons_supprimes) | set(doublons_choisis) | set(inclusions_script)
    for numero, (fid, reference) in enumerate(origine.items(), 1):
        avancer(tr('Contrôle final et sens des polygones'), numero, len(origine), 85, 86, reference)
        geom = QgsGeometry(modifs.get(fid, geoms.get(fid, reference.geometry())))
        if fid not in exclus_script and fid not in invalides:
            geom = nettoyer_resultat_script(fid, geom)
            geom, action, detail_sens = cs.orienter(geom)
            if action == 'CORRIGE':
                orientations_script.add(fid)
            if bytes(geom.asWkb()) != bytes(reference.geometry().asWkb()):
                modifs[fid] = QgsGeometry(geom)
            if action == 'ECHEC':
                incidents.append('Sens non corrigé FID {} : {}'.format(fid, detail_sens))
        else:
            action, detail_sens = 'NON_APPLICABLE', 'Objet supprimé ou géométrie invalide'
        try:
            controle_script = cs.controle(geom)
        except Exception as erreur:
            controle_script = ['INDETERMINE', 'INDETERMINE', 'ERREUR', str(erreur)]
        script_final[fid] = dict(zip(('sens_trace', 'sens_trous', 'geom_etat', 'sens_detail'), controle_script))
        script_final[fid].update(sens_act=action, sens_info=detail_sens)

    # Le statut final vient des corrections EFFECTIVES et des ambiguïtés
    # restantes. Une décision validée prévaut sur le diagnostic préalable
    # pour ce couple précis ; les autres conflits non résolus subsistent.
    for fid in statuts:
        if fid in modifs:
            statuts[fid] = 'oui'
        elif causes_ambigues[fid]:
            statuts[fid] = 'ambigu_' + '_'.join(
                str(oid) for oid in sorted(causes_ambigues[fid]))
        elif fid in invalides:
            statuts[fid] = 'ambigu'
        elif statuts[fid] == 'ambigu':
            statuts[fid] = 'non'

    # éviter la couche mémoire, qui peut refuser des enregistrements
    # (types/contraintes des champs issus d'un GeoPackage).
    # 1) Copier la source sur disque. 2) Vérifier intégralement les attributs.
    # 3) Modifier uniquement les géométries requises et le nouveau champ.
    srcpath = Path(COUCHE.source().split('|')[0])
    outdir = srcpath.parent if srcpath.is_file() else Path.home()
    if mode_retraits == 'file':
        outdir = Path(chemin_retraits).parent
    extension_finale = format_retraits[1] if mode_retraits == 'file' else '.gpkg'
    nom_corrige, sortie_finale = nom_sortie_corrigee(COUCHE.name(), outdir, extension_finale)
    # Copie de travail privée : les contrôles existants restent indépendants
    # des limitations attributaires et des FID des formats de livraison.
    _dossier_travail = tempfile.TemporaryDirectory(prefix='correction_geometrique_')
    sortie = Path(_dossier_travail.name) / 'travail.gpkg'
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.overrideGeometryType = QgsWkbTypes.MultiPolygon
    options.forceMulti = True
    options.layerName = 'polygones_corriges'
    options.fileEncoding = 'UTF-8'
    options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
    # GPKG réserve fid à sa clé primaire interne. Le champ source nommé fid
    # est renommé fid_source durant l'export pour éviter UNIQUE constraint.
    # La valeur de fid_source ne sera jamais une nouvelle clé primaire.
    champ_fid = next((n for n in champs if n.casefold() == 'fid'), None)
    if champ_fid is not None:
        options.attributes = list(range(len(COUCHE.fields())))
        options.attributesExportNames = [
            'fid_source' if champ == champ_fid else champ
            for champ in champs
        ]
    avancer(tr('Copie intégrale de la couche source'), 1, 1, 85, 91)
    # Ne pas masquer la source ou considérer le fichier comme validé avant contrôle.
    progression.setCancelButton(None)
    QApplication.processEvents()
    ret = QgsVectorFileWriter.writeAsVectorFormatV3(
        COUCHE, str(sortie), QgsProject.instance().transformContext(), options)
    if ret[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(tr('Export source impossible : {}').format(ret))
    enregistre = QgsVectorLayer(str(sortie) + '|layername=polygones_corriges',
                                nom_corrige, 'ogr')
    if not enregistre.isValid() or enregistre.featureCount() != len(source):
        raise RuntimeError(tr('Copie du GeoPackage incomplète : {}').format(sortie))

    # Vérification attributaire stricte, avec le renommage fid -> fid_source.
    # Ne jamais comparer le fid nouvellement généré avec le fid d'origine.
    champs_source = COUCHE.fields().names()
    champs_sortie = enregistre.fields().names()
    correspondance_champs = {
        nom: ('fid_source' if nom == champ_fid else nom)
        for nom in champs_source
    }
    def empreinte(v):
        return tuple((type(x).__name__, repr(x)) for x in v)
    def donnees(feat, noms):
        return empreinte([feat[n] for n in noms])
    noms_sortie = [correspondance_champs[n] for n in champs_source]
    if set(noms_sortie) - set(champs_sortie):
        raise RuntimeError(tr('Champs source absents de la copie : {}').format(
            sorted(set(noms_sortie) - set(champs_sortie))))
    avant_attributs = Counter(donnees(f, champs_source) for f in source)
    apres_attributs = Counter(donnees(f, noms_sortie) for f in enregistre.getFeatures())
    if avant_attributs != apres_attributs:
        raise RuntimeError(tr('Attributs non préservés lors de la copie : {}').format(sortie))

    # ne plus chercher de correspondance par fid/id/gid.
    # Seules les données attributaires et la géométrie source servent au
    # rapprochement entre les entités et la copie. Les valeurs fid_source
    # restent conservées comme attributs, sans servir de clé de rapprochement.
    mapping = None
    if mapping is None:
        def memes_geometries(a, b):
            ga, gb = QgsGeometry(a), QgsGeometry(b)
            if not ga.isMultipart():
                ga.convertToMultiType()
            if not gb.isMultipart():
                gb.convertToMultiType()
            return bytes(ga.asWkb()) == bytes(gb.asWkb()) or ga.equals(gb)

        def signature(feat, noms):
            g = feat.geometry()
            b = g.boundingBox()
            mesure = (b.xMinimum(), b.yMinimum(), b.xMaximum(),
                      b.yMaximum(), g.area())
            return (donnees(feat, noms), tuple(round(v, 5) for v in mesure))

        groupes_src = defaultdict(list)
        groupes_dst = defaultdict(list)
        for feat in source:
            groupes_src[signature(feat, champs_source)].append(feat)
        for feat in enregistre.getFeatures():
            groupes_dst[signature(feat, noms_sortie)].append(feat)
        mapping = {}
        for cle_sig, lignes_src in groupes_src.items():
            lignes_dst = groupes_dst[cle_sig]
            if len(lignes_src) != len(lignes_dst):
                raise RuntimeError(tr('Correspondance impossible : attributs répétés.'))
            disponibles = list(lignes_dst)
            for feat_src in sorted(lignes_src, key=lambda f: f.id()):
                correspondances = [f for f in disponibles
                                  if memes_geometries(f.geometry(), feat_src.geometry())]
                if not correspondances:
                    raise RuntimeError(tr('Géométrie source FID {} introuvable dans la copie.').format(feat_src.id()))
                # Des copies parfaitement indiscernables (TOUS les attributs
                # source et géométries égaux) sont interchangeables dans cette
                # copie de travail. Affecter une bijection déterministe permet
                # de corriger le représentant puis supprimer ses copies.
                # Leurs plans peuvent désormais différer après dédoublonnage.
                correspondances.sort(key=lambda f: f.id())
                choisi = correspondances[0]
                mapping[feat_src.id()] = choisi.id()
                disponibles.remove(choisi)
        if len(mapping) != len(source) or len(set(mapping.values())) != len(source):
            raise RuntimeError(tr('Correspondance des entités non bijective.'))
        print('Correspondance vérifiée par attributs et géométries.')

    # Si la source ne comportait aucun champ fid, fid_source est créé ici,
    # puis renseigné avec les FID QGIS originaux grâce au mapping validé.
    nouveaux_champs = [QgsField('correction', CHAMP_TEXTE)]
    champs_script = {}
    noms_utilises = {nom.casefold() for nom in enregistre.fields().names()} | {'correction', 'fid_source'}
    for cle in ('sens_trace', 'sens_trous', 'geom_etat', 'geom_avant', 'reparation', 'sens_act'):
        nom_champ, numero = cle, 2
        while nom_champ.casefold() in noms_utilises:
            nom_champ = '{}_{}'.format(cle, numero)
            numero += 1
        noms_utilises.add(nom_champ.casefold())
        champs_script[cle] = nom_champ
        nouveaux_champs.append(QgsField(nom_champ, CHAMP_TEXTE, len=40))
    if champ_fid is None:
        nouveaux_champs.append(QgsField('fid_source', CHAMP_ENTIER))
    if not enregistre.dataProvider().addAttributes(
            nouveaux_champs):
        raise RuntimeError(tr('Impossible de créer le champ correction : {}').format(sortie))
    enregistre.updateFields()
    index_correction = enregistre.fields().indexFromName('correction')
    indices_script = {cle: enregistre.fields().indexFromName(nom)
                      for cle, nom in champs_script.items()}
    if any(i < 0 for i in indices_script.values()):
        raise RuntimeError(tr('Champ de suivi du script absent après création.'))

    def attributs_script(fid):
        donnees_script = dict(script_final[fid])
        donnees_script['geom_avant'] = script_avant[fid]['geom_avant']
        donnees_script['reparation'] = script_avant[fid]['reparation']
        if fid in nettoyages_finaux and donnees_script['reparation'] == 'NON_NECESSAIRE':
            donnees_script['reparation'] = 'RESIDUS_SUPPRIMES'
        return {indice: donnees_script[cle] for cle, indice in indices_script.items()}
    index_fid_source = enregistre.fields().indexFromName('fid_source')
    if index_fid_source < 0:
        raise RuntimeError(tr('Champ fid_source absent du résultat.'))
    if index_correction < 0:
        raise RuntimeError(tr('Champ correction absent après création.'))

    # Écritures en lots, avec contrôles des réponses du fournisseur.
    # Une découpe peut convertir Polygon vers MultiPolygon : couche cible GPKG
    # doit accueillir une géométrie multipartie (normalisée si nécessaire).
    dp = enregistre.dataProvider()
    items = list(mapping.items())
    BATCH = 100
    for debut_lot in range(0, len(items), BATCH):
        partie = items[debut_lot:debut_lot + BATCH]
        changements_attributs = {
            dst: ({index_correction: statuts[src]} if champ_fid is not None else
                  {index_correction: statuts[src], index_fid_source: src})
            for src, dst in partie
        }
        for src, dst in partie:
            changements_attributs[dst].update(attributs_script(src))
        changements_geometries = {}
        for fid_source, fid_sortie in partie:
            if fid_source in modifs:
                g = multipolygone_sur(modifs[fid_source])
                if not g.isMultipart() and not g.convertToMultiType():
                    raise RuntimeError(tr('Conversion MultiPolygon impossible : {}').format(fid_source))
                changements_geometries[fid_sortie] = g
        if changements_geometries and not dp.changeGeometryValues(changements_geometries):
            raise RuntimeError(tr('Échec d’écriture des géométries au lot {} : {}').format(
                debut_lot // BATCH + 1, dp.lastError()))
        if not dp.changeAttributeValues(changements_attributs):
            raise RuntimeError(tr('Échec d’écriture des statuts au lot {} : {}').format(
                debut_lot // BATCH + 1, dp.lastError()))
        avancer(tr('Enregistrement des corrections (lot)'),
                min(debut_lot+BATCH, len(items)), len(items), 91, 97)
    enregistre.reload()

    # Vérification après réouverture indépendante du GeoPackage.
    controle = QgsVectorLayer(str(sortie) + '|layername=polygones_corriges',
                              'controle_sortie', 'ogr')
    if not controle.isValid() or controle.featureCount() != len(source):
        raise RuntimeError(tr('Résultat disque incomplet : {}').format(sortie))
    verifies = list(controle.getFeatures())
    # Contrôle du WKB réel de CHAQUE objet : le type déclaré du GPKG ne suffit pas.
    for objet in verifies:
        g = objet.geometry()
        if (g.isNull() or g.isEmpty() or
                QgsWkbTypes.flatType(g.wkbType()) != QgsWkbTypes.MultiPolygon):
            raise RuntimeError(tr('Géométrie incompatible avec un SHP polygonal, FID {} : {}').format(
                objet.id(), g.wkbType()))
    if Counter(donnees(f, noms_sortie) for f in verifies) != avant_attributs:
        raise RuntimeError(tr('ATTRIBUTS MODIFIÉS lors de l’export, ne pas utiliser : {}').format(sortie))
    counts = Counter(f['correction'] for f in verifies)
    if counts != Counter(statuts.values()):
        raise RuntimeError(tr('Statuts de correction incorrects après sauvegarde : {}').format(sortie))
    verif_id = {f.id(): f for f in verifies}
    # retrait du contrôle individuel de correspondance par FID.
    # Le contrôle global des attributs et des corrections géométriques subsiste.
    for fid_source, fid_sortie in mapping.items():
        if fid_source not in modifs:
            continue
        final = verif_id.get(fid_sortie)
        if final is None:
            raise RuntimeError(tr('Entité corrigée introuvable : {}').format(fid_source))
        geom_voulue = modifs[fid_source]
        geom_finale = final.geometry()
        if geom_finale.isNull() or not geom_finale.isGeosValid():
            raise RuntimeError(tr('Géométrie corrigée invalide : {}').format(fid_source))
        if operation_geos_robuste(geom_finale, geom_voulue, 'symDifference').area() > max(TOL_AIRE, geom_voulue.area()*1e-9):
            raise RuntimeError(tr('Géométrie corrigée non conservée : {}').format(fid_source))
    for src, dst in mapping.items():
        for indice, attendu in attributs_script(src).items():
            if verif_id[dst][indice] != attendu:
                raise RuntimeError(tr('Suivi script non conservé, FID {}').format(src))

    # Supprimer les doublons et les inclusions à attributs métier identiques.
    # Ne jamais supprimer une entite seulement parce qu'elle est ambigue.
    # Les découpes peuvent elles-mêmes rendre identiques deux objets qui
    # étaient distincts au départ : les traiter dès ce passage.
    exclus = set(doublons_supprimes) | set(doublons_choisis) | set(inclusions_script)
    geometries_finales = {fid: modifs.get(fid, geom) for fid, geom in geoms.items()
                         if fid not in exclus}
    nouveaux_doublons, _ = detecter_doublons_geometriques(
        geometries_finales, origine, champs_reglementaires, partition_champ,
        suivi=lambda n, total, fid: avancer(tr('Vérification finale des doublons'),
                                            n, total, 94, 94, origine[fid]))
    doublons_supprimes.update(nouveaux_doublons)
    # Garder dans le rapport un représentant réellement livré.
    for fid in list(doublons_supprimes):
        ref = doublons_supprimes[fid]
        while ref in doublons_supprimes or ref in doublons_choisis:
            ref = (doublons_supprimes[ref] if ref in doublons_supprimes
                   else doublons_choisis[ref])
        doublons_supprimes[fid] = ref
    toutes_suppressions = dict(inclusions_script)
    toutes_suppressions.update(doublons_supprimes)
    toutes_suppressions.update(doublons_choisis)
    for table in (inclusions_script, doublons_supprimes, doublons_choisis):
        for fid, ref in list(table.items()):
            visites = {fid}
            while ref in toutes_suppressions:
                if ref in visites:
                    raise RuntimeError(tr('Cycle dans le suivi des objets supprimés, FID {}').format(fid))
                visites.add(ref)
                ref = toutes_suppressions[ref]
            table[fid] = ref

    ids_supprimes = sorted(set(doublons_supprimes) | set(doublons_choisis) | set(inclusions_script))
    ids_sortie_doublons = [mapping[fid] for fid in ids_supprimes]
    if ids_sortie_doublons:
        if not dp.deleteFeatures(ids_sortie_doublons):
            raise RuntimeError(tr('Suppression des doublons echouee : {}').format(dp.lastError()))
        enregistre.reload()
        attendu = len(source) - len(ids_supprimes)
        if enregistre.featureCount() != attendu:
            raise RuntimeError(tr('Nombre d entites incorrect apres dedoublonnage : {} / {}').format(
                enregistre.featureCount(), attendu))
        restant = Counter(donnees(f, champs_source) for f in source
                          if f.id() not in ids_supprimes)
        final_attributs = Counter(donnees(f, noms_sortie)
                                 for f in enregistre.getFeatures())
        if restant != final_attributs:
            raise RuntimeError(tr('Attributs restants modifies apres suppression des doublons.'))
        for f in ids_supprimes:
            if enregistre.getFeature(mapping[f]).isValid():
                raise RuntimeError(tr('FID doublon encore present dans la sortie : {}').format(f))

    # Rapport des doublons, independant du fichier de controle des surfaces.
    chemin_rapport_doublons = sortie_finale.with_name(sortie_finale.stem + '_DOUBLONS.csv')
    if doublons_supprimes or doublons_choisis:
        with chemin_rapport_doublons.open('w', newline='', encoding='utf-8-sig') as fh:
            writer = csv.writer(fh, delimiter=';')
            writer.writerow(['partition', 'fid_conserve', 'fid_supprime',
                             'surface_m2', 'motif'])
            for fid, ref in sorted(doublons_supprimes.items()):
                writer.writerow([origine[fid][partition_champ] if partition_champ else '',
                                 ref, fid, '{:.6f}'.format(origine[fid].geometry().area()),
                                 'Surfaces GEOS identiques et attributs reglementaires identiques'])
            for fid, ref in sorted(doublons_choisis.items()):
                writer.writerow([origine[fid][partition_champ] if partition_champ else '',
                                 ref, fid, '{:.6f}'.format(origine[fid].geometry().area()),
                                 'Géométrie identique ; attributs divergents ; choix utilisateur'])
    chemin_rapport_reparations = sortie_finale.with_name(sortie_finale.stem + '_REPARATIONS.csv')
    if reparations or echecs_reparation:
        with chemin_rapport_reparations.open('w', newline='', encoding='utf-8-sig') as fh:
            rapport = csv.writer(fh, delimiter=';')
            rapport.writerow(['fid_source', 'etat', 'surface_avant_m2', 'surface_apres_m2', 'detail'])
            for fid in sorted(set(reparations) | set(echecs_reparation)):
                g = reparations.get(fid)
                rapport.writerow([fid, 'repare' if g is not None else 'echec',
                                  origine[fid].geometry().area(),
                                  g.area() if g is not None else '',
                                  echecs_reparation.get(fid, '') or script_avant[fid]['rep_detail']])
    print('DOUBLONS SUPPRIMES DE LA COPIE : {} | rapport : {}'.format(
        len(doublons_supprimes) + len(doublons_choisis), chemin_rapport_doublons))

    # couche TEMPORAIRE de contrôle, construite seulement à partir
    # des géométries originales et des géométries effectivement enregistrées.
    # Aucun attribut ou géométrie de la couche source n'est modifié.
    # La découpe réelle est ORIGINE - SORTIE, et non la simple différence
    # entre deux polygones candidats.
    nom_controle = 'DECOUPES - {}'.format(COUCHE.name())
    couche_retraits = QgsVectorLayer(
        'MultiPolygon?crs={}'.format(COUCHE.crs().authid()),
        nom_controle, 'memory')
    if not couche_retraits.isValid():
        raise RuntimeError(tr('Impossible de créer la couche temporaire de contrôle.'))
    fournisseur_retraits = couche_retraits.dataProvider()
    if not fournisseur_retraits.addAttributes([
        QgsField('fid_source', CHAMP_ENTIER),
        QgsField('fid_autre', CHAMP_ENTIER),
        QgsField('couche_corr', CHAMP_TEXTE, len=254),
        QgsField('partition', CHAMP_TEXTE, len=254),
        QgsField('surface_m2', CHAMP_REEL, len=20, prec=6),
        QgsField('etat', CHAMP_TEXTE, len=20)
    ]):
        raise RuntimeError(tr('Impossible de créer les champs de la couche de contrôle.'))
    couche_retraits.updateFields()
    objets_retraits = []
    surface_totale = 0.0
    avancer(tr('Création de CORRECTIONS_GEOMETRIQUES : surfaces retirées'), 0,
            max(1, len(mapping)), 94, 95)
    for indice_controle, (id_source, id_dest) in enumerate(mapping.items(), 1):
        if indice_controle == 1 or indice_controle % 25 == 0 or indice_controle == len(mapping):
            avancer(tr('Création de CORRECTIONS_GEOMETRIQUES : surfaces retirées'),
                    indice_controle, len(mapping), 94, 95, origine[id_source])
        if id_source not in modifs or id_source in ids_supprimes:
            continue
        avant = reparations.get(id_source, origine[id_source].geometry())
        apres = verif_id[id_dest].geometry()
        # Les deux géométries sont validées plus haut.
        retire = operation_geos_robuste(avant, apres, 'difference')
        if retire.isNull() or not retire.isGeosValid():
            raise RuntimeError(tr('Différence de contrôle invalide, FID {}').format(id_source))
        aire = retire.area() if not retire.isEmpty() else 0.0
        if aire <= TOL_AIRE:
            if id_source in reparations or id_source in orientations_script or id_source in nettoyages_finaux:
                # Une réparation ou réorientation seule n'est pas une découpe.
                continue
            raise RuntimeError(
                tr('Statut corrigé sans surface retirée significative, FID {}').format(id_source))
        # Une correction ne doit jamais ajouter de surface hors du polygone source.
        aire_ajoutee = operation_geos_robuste(apres, avant, 'difference').area()
        if aire_ajoutee > max(tolerance_precision(avant), avant.area() * 1e-9):
            raise RuntimeError(tr('Correction ajoutant de la surface, FID {}').format(id_source))
        retire = multipolygone_sur(retire)
        if QgsWkbTypes.geometryType(retire.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise RuntimeError(tr('Surface retirée non polygonale, FID {}').format(id_source))
        if not retire.isMultipart() and not retire.convertToMultiType():
            raise RuntimeError(tr('Conversion du retrait en multipolygone impossible.'))
        f = QgsFeature(couche_retraits.fields())
        f.setGeometry(retire)
        valeur_partition = origine[id_source][partition_champ] if partition_champ else None
        partition_texte = (None if valeur_partition is None or
                           str(valeur_partition) in ('NULL', '') else str(valeur_partition))
        id_attribut = origine[id_source][champ_fid] if champ_fid else id_source
        f.setAttributes([id_attribut, None, COUCHE.name(), partition_texte, aire, 'retire'])
        objets_retraits.append(f)
        surface_totale += aire
    # une ambiguïté de recouvrement est représentée par L'INTERSECTION
    # RÉELLE DE DEUX POLYGONES, jamais par l'emprise entière d'un polygone.
    # Conserver un seul objet par paire de FID, sans doublonner les surfaces
    # lors des listes symétriques de causes_ambigues.
    objets_ambigus = []
    paires_ambigues = set()
    avancer(tr('Création de CORRECTIONS_GEOMETRIQUES : intersections ambiguës'),
            0, max(1, len(causes_ambigues)), 95, 96)
    for indice_ambigu, (fid, autres) in enumerate(causes_ambigues.items(), 1):
        if indice_ambigu == 1 or indice_ambigu % 25 == 0 or indice_ambigu == len(causes_ambigues):
            avancer(tr('Création de CORRECTIONS_GEOMETRIQUES : intersections ambiguës'),
                    indice_ambigu, len(causes_ambigues), 95, 96)
        if fid not in geoms:
            continue
        for oid in autres:
            if oid not in geoms or fid == oid:
                continue
            paire = (min(fid, oid), max(fid, oid))
            if paire in paires_ambigues:
                continue
            paires_ambigues.add(paire)
            a, b = paire
            # Les opérations de diagnostic restent sur les géométries
            # originales, comme les anciens masques par couple.
            try:
                commune = operation_geos_robuste(geoms[a], geoms[b], 'intersection')
                if commune.isNull() or commune.isEmpty():
                    continue
                surface = commune.area()
                if surface <= TOL_AIRE:
                    continue
                if not commune.isGeosValid():
                    raise ValueError('Intersection invalide')
                commune = multipolygone_sur(commune)
                if QgsWkbTypes.geometryType(commune.wkbType()) != QgsWkbTypes.PolygonGeometry:
                    continue
                if not commune.isMultipart() and not commune.convertToMultiType():
                    raise ValueError('Conversion MultiPolygon impossible')
                valeur_partition = (origine[a][partition_champ]
                                    if partition_champ else None)
                part = (None if valeur_partition is None or
                        str(valeur_partition) in ('NULL', '')
                        else str(valeur_partition))
                id_a = origine[a][champ_fid] if champ_fid else a
                id_b = origine[b][champ_fid] if champ_fid else b
                f = QgsFeature(couche_retraits.fields())
                f.setGeometry(commune)
                f.setAttributes([id_a, id_b, COUCHE.name(), part,
                                 surface, 'ambigu'])
                objets_ambigus.append(f)
            except Exception as exc:
                incidents.append('Intersection ambiguë {} / {} : {}'.format(a, b, exc))
                # Ne pas substituer le polygone entier si le calcul échoue.
                print('AVERTISSEMENT : intersection ambiguë non représentée : '
                      'FID {} / {} : {}'.format(a, b, exc))
    # La couche des découpes représente exclusivement ORIGINE - RÉSULTAT.
    # Aucun polygone entier réparé, ni intersection non corrigée.
    objets_controle = objets_retraits
    avancer(tr('Insertion dans CORRECTIONS_GEOMETRIQUES'), 0, 1, 96, 97)
    if objets_controle:
        insertion = fournisseur_retraits.addFeatures(objets_controle)
        if not insertion[0] or couche_retraits.featureCount() != len(objets_controle):
            raise RuntimeError(tr('Insertion incomplète dans CORRECTIONS_GEOMETRIQUES.'))
    couche_retraits.updateExtents()
    avancer(tr('Couche CORRECTIONS_GEOMETRIQUES préparée'), 1, 1, 96, 97)
    # Afficher en rouge avec un contour bien visible, au-dessus de la sortie.
    symbole = QgsFillSymbol.createSimple({
        'color': '255,40,40,135',
        'outline_color': '190,0,0,255',
        'outline_width': '0.45',
        'outline_width_unit': 'MM'
    })
    categories = [
        QgsRendererCategory('retire', symbole, 'Emprise du trou / surface réellement retirée')
    ]
    couche_retraits.setRenderer(QgsCategorizedSymbolRenderer('etat', categories))
    # sortie facultative sur disque des retraits et ambiguïtés.
    couche_retraits_finale = couche_retraits
    if mode_retraits == 'file':
        if not objets_controle:
            print('Aucune surface retirée : aucune couche des découpes créée.')
        else:
            chemin_final = Path(chemin_retraits)
            # Remplacement déjà confirmé dans la fenêtre de paramétrage.
            # Le writer utilise CreateOrOverwriteFile ci-dessous.
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = format_retraits[0]
            opts.fileEncoding = 'UTF-8'
            opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
            if opts.driverName == 'GPKG':
                opts.layerName = 'CORRECTIONS_GEOMETRIQUES'
            avancer(tr('Enregistrement du fichier CORRECTIONS_GEOMETRIQUES'),
                    1, 1, 97, 97)
            rep = QgsVectorFileWriter.writeAsVectorFormatV3(
                couche_retraits, str(chemin_final),
                QgsProject.instance().transformContext(), opts)
            if rep[0] != QgsVectorFileWriter.WriterError.NoError:
                raise RuntimeError(tr('Export du contrôle impossible : {}').format(rep))
            adresse = str(chemin_final)
            if opts.driverName == 'GPKG':
                adresse += '|layername=CORRECTIONS_GEOMETRIQUES'
            couche_retraits_finale = QgsVectorLayer(
                adresse, nom_controle, 'ogr')
            if (not couche_retraits_finale.isValid() or
                    couche_retraits_finale.featureCount() != len(objets_controle)):
                raise RuntimeError(tr('Couche de contrôle enregistrée incomplète : {}').format(chemin_final))
            # Contrôle comparatif des surfaces et des FID après écriture.
            surfaces_rechargees = sum(f.geometry().area()
                                      for f in couche_retraits_finale.getFeatures()
                                      if f['etat'] == 'retire')
            if abs(surfaces_rechargees - surface_totale) > max(
                    TOL_AIRE, surface_totale * 1e-9):
                raise RuntimeError(tr('Surface du fichier de contrôle incohérente.'))
            origine_ids = sorted((f['fid_source'], f['etat']) for f in objets_controle)
            dest_ids = sorted((f['fid_source'], f['etat']) for f in couche_retraits_finale.getFeatures())
            if origine_ids != dest_ids:
                raise RuntimeError(tr('FID source altérés dans le fichier de contrôle.'))
            couche_retraits_finale.setRenderer(couche_retraits.renderer().clone())
    creer_couche_corrigee = bool(modifs or ids_supprimes)
    if creer_couche_corrigee:
        # Livrer la couche corrigée dans le format et le dossier choisis.
        opts_final = QgsVectorFileWriter.SaveVectorOptions()
        opts_final.driverName = format_retraits[0] if mode_retraits == 'file' else 'GPKG'
        opts_final.fileEncoding = 'UTF-8'
        if opts_final.driverName == 'ESRI Shapefile':
            opts_final.layerOptions = ['RESIZE=YES']
        opts_final.overrideGeometryType = QgsWkbTypes.MultiPolygon
        opts_final.forceMulti = True
        opts_final.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        # Exclure uniquement la clé interne GPKG : fid_source reste conservé.
        indices_final = [i for i, champ in enumerate(enregistre.fields())
                         if champ.name().casefold() != 'fid']
        opts_final.attributes = indices_final
        if opts_final.driverName == 'GPKG':
            opts_final.layerName = 'polygones_corriges'
        rep_final = QgsVectorFileWriter.writeAsVectorFormatV3(
            enregistre, str(sortie_finale), QgsProject.instance().transformContext(), opts_final)
        if rep_final[0] != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(tr('Export de la couche corrigée impossible : {}').format(rep_final))
        adresse_finale = str(sortie_finale)
        if opts_final.driverName == 'GPKG':
            adresse_finale += '|layername=polygones_corriges'
        livraison = QgsVectorLayer(adresse_finale, enregistre.name(), 'ogr')
        if not livraison.isValid() or livraison.featureCount() != enregistre.featureCount():
            raise RuntimeError(tr('Couche corrigée exportée incomplète : {}').format(sortie_finale))
        # Les noms SHP peuvent être raccourcis : comparer par position et contenu,
        # sans supposer que les FID générés par les fournisseurs sont identiques.
        noms_attendus = [enregistre.fields()[i].name() for i in indices_final]
        noms_livres = [n for n in livraison.fields().names() if n.casefold() != 'fid']
        if len(noms_attendus) != len(noms_livres):
            raise RuntimeError(tr('Nombre de champs modifié dans la couche corrigée exportée.'))
        est_shp = opts_final.driverName == 'ESRI Shapefile'

        def valeur_livraison(valeur):
            # NULL QVariant n'est pas la chaîne littérale "NULL".
            if valeur is None or QgsVariantUtils.isNull(valeur):
                return None
            # Un entier LongLong DBF large peut être relu comme double par OGR.
            # Decimal compare exactement 0 et 0.0, sans arrondir les identifiants.
            # Ne jamais convertir les chaînes : "04004" garde ses zéros initiaux.
            if isinstance(valeur, (int, float, Decimal)) and not isinstance(valeur, bool):
                nombre = Decimal(str(valeur))
                return ('nombre', nombre) if nombre.is_finite() else ('non_fini', str(nombre))
            texte = str(valeur)
            if est_shp and isinstance(valeur, str):
                # DBF ne distingue pas NULL d'un champ texte vide et supprime
                # les espaces de remplissage à droite. Ne jamais tronquer un texte.
                texte = texte.rstrip(' ')
                return texte if texte else None
            return texte

        def cle_livraison(objet, noms):
            return tuple(valeur_livraison(objet[n]) for n in noms)

        attendus = defaultdict(list)
        references_export = defaultdict(list)
        for objet in enregistre.getFeatures():
            attendus[cle_livraison(objet, noms_attendus)].append(objet.geometry())
            references_export[valeur_livraison(objet['fid_source'])].append(objet)

        def erreur_export(objet, candidats):
            # Diagnostic par identifiant conservé, pas par FID généré du SHP.
            id_source = objet['fid_source']
            references = references_export.get(valeur_livraison(id_source), [])
            lignes = []
            if not candidats:
                for reference in references:
                    for nom_avant, nom_apres in zip(noms_attendus, noms_livres):
                        avant, apres = reference[nom_avant], objet[nom_apres]
                        if valeur_livraison(avant) != valeur_livraison(apres):
                            lignes.append([id_source, 'attribut', nom_avant, nom_apres,
                                           repr(avant), repr(apres)])
            else:
                for g in candidats:
                    delta = operation_geos_robuste(geometrie, g, 'symDifference')
                    detail = ('écart surfacique m² : {}'.format(delta.area())
                              if not delta.isNull() else 'différence géométrique non calculable')
                    lignes.append([id_source, 'geometrie', '', '',
                                   'surface attendue : {}'.format(g.area()), detail])
            if not lignes:
                lignes.append([id_source, 'correspondance', '', '',
                               'Aucune correspondance bijective', 'FID export : {}'.format(objet.id())])
            chemin_diagnostic = sortie_finale.with_name(sortie_finale.stem + '_ERREUR_EXPORT.csv')
            with chemin_diagnostic.open('w', newline='', encoding='utf-8-sig') as fh:
                rapport = csv.writer(fh, delimiter=';')
                rapport.writerow(['fid_source', 'type', 'champ_avant', 'champ_apres', 'attendu', 'obtenu'])
                rapport.writerows(lignes)
            for ligne in lignes[:10]:
                print('ERREUR EXPORT :', ligne)
            raise RuntimeError(tr('Contrôle export échoué, fid_source {} : {}. Détail : {}').format(
                id_source, 'attributs' if not candidats else 'géométrie', chemin_diagnostic))
        interpretations_shp = []
        for objet in livraison.getFeatures():
            geometrie = objet.geometry()
            if (geometrie.isNull() or geometrie.isEmpty() or
                    QgsWkbTypes.flatType(geometrie.wkbType()) not in
                    (QgsWkbTypes.Polygon, QgsWkbTypes.MultiPolygon)):
                raise RuntimeError(tr('Géométrie non polygonale dans la livraison, FID {}').format(objet.id()))
            if geometrie.isGeosValid():
                # Les statuts ajoutés sont courts ; les détails restent dans le CSV.
                # Vérifier aussi la convention réelle écrite par le pilote.
                controle_sens_livre = cs.controle(geometrie)
                if controle_sens_livre[0] != 'HORAIRE' or controle_sens_livre[1] not in ('ANTIHORAIRE', 'SANS_TROU'):
                    raise RuntimeError(tr('Sens non conforme après export, fid_source {} : {}').format(
                        objet['fid_source'], controle_sens_livre[3]))
            candidats = attendus.get(cle_livraison(objet, noms_livres), [])
            position = next((i for i, g in enumerate(candidats)
                             if geometries_export_equivalentes(g, geometrie)), None)
            if position is None:
                erreur_export(objet, candidats)
            if not geometrie.isGeosValid():
                interpretations_shp.append(objet['fid_source'])
            candidats.pop(position)
        if any(attendus.values()):
            raise RuntimeError(tr('Entités manquantes dans la livraison corrigée.'))
        if interpretations_shp:
            rapport_interpretation = sortie_finale.with_name(sortie_finale.stem + '_LECTURE_ANNEAUX.csv')
            with rapport_interpretation.open('w', newline='', encoding='utf-8-sig') as fh:
                rapport = csv.writer(fh, delimiter=';')
                rapport.writerow(['fid_source', 'controle', 'observation'])
                for fid in interpretations_shp:
                    rapport.writerow([fid, 'surfaces_identiques_apres_makeValid',
                                      'Regroupement des anneaux à vérifier avec le lecteur SHP utilisé'])
            print('LECTURE SHP : {} interprétations des anneaux normalisées pour le contrôle ; '
                  'surfaces strictement identiques. Rapport : {}'.format(
                      len(interpretations_shp), rapport_interpretation))
        enregistre = livraison
        sortie = sortie_finale
    else:
        # La copie privée de travail n'est jamais livrée comme résultat inchangé.
        enregistre = None
        sortie = None
    anomalies_script = [fid for fid, etat in script_final.items()
                        if etat['geom_etat'] != 'VALIDE' or etat['sens_act'] == 'ECHEC'
                        or script_avant[fid]['reparation'] in ('ECHEC', 'IMPOSSIBLE')]
    if creer_couche_corrigee or anomalies_script:
        chemin_rapport_script = sortie_finale.with_name(sortie_finale.stem + '_SCRIPT.csv')
        with chemin_rapport_script.open('w', newline='', encoding='utf-8-sig') as fh:
            rapport = csv.writer(fh, delimiter=';')
            rapport.writerow(['fid_source', 'partition', 'etat_livraison', 'fid_conserve',
                'geom_avant', 'reparation', 'detail_reparation', 'nettoyages_apres_decoupe',
                'geom_finale', 'sens_exterieurs', 'sens_trous', 'action_sens', 'detail_sens', 'detail_anneaux'])
            for fid in sorted(origine):
                avant, final = script_avant[fid], script_final[fid]
                if fid in inclusions_script:
                    etat, ref = 'inclusion_identique_supprimee', inclusions_script[fid]
                elif fid in doublons_supprimes:
                    etat, ref = 'doublon_identique_supprime', doublons_supprimes[fid]
                elif fid in doublons_choisis:
                    etat, ref = 'doublon_supprime_par_choix', doublons_choisis[fid]
                else:
                    etat, ref = 'conserve', ''
                rapport.writerow([fid, origine[fid][partition_champ] if partition_champ else '', etat, ref,
                    avant['geom_avant'], avant['reparation'], avant['rep_detail'],
                    ' | '.join(avant['nettoyages']), final['geom_etat'], final['sens_trace'],
                    final['sens_trous'], final['sens_act'], final['sens_info'], final['sens_detail']])
        print('Rapport du script intégré :', chemin_rapport_script)
    if inclusions_script:
        chemin_inclusions = sortie_finale.with_name(sortie_finale.stem + '_INCLUSIONS.csv')
        with chemin_inclusions.open('w', newline='', encoding='utf-8-sig') as fh:
            rapport = csv.writer(fh, delimiter=';')
            rapport.writerow(['fid_source_supprime', 'fid_conserve', 'motif'])
            for fid, ref in sorted(inclusions_script.items()):
                rapport.writerow([fid, ref, 'Couverture complète et attributs métier identiques'])
        print('Rapport des inclusions supprimées :', chemin_inclusions)
    if orientations_script:
        chemin_sens = sortie_finale.with_name(sortie_finale.stem + '_SENS.csv')
        with chemin_sens.open('w', newline='', encoding='utf-8-sig') as fh:
            rapport = csv.writer(fh, delimiter=';')
            rapport.writerow(['fid_source', 'orientation', 'etat'])
            for fid in sorted(orientations_script):
                rapport.writerow([fid, 'extérieur horaire / trous antihoraires',
                                  'supprime' if fid in ids_supprimes else 'sens_corrige'])
        print('Rapport des sens corrigés :', chemin_sens)
    avancer(tr('Export et correction vérifiés'), 1, 1, 97, 100)
    progression.close()
    if creer_couche_corrigee:
        QgsProject.instance().addMapLayer(enregistre)
        node = QgsProject.instance().layerTreeRoot().findLayer(COUCHE.id())
        if node:
            node.setItemVisibilityChecked(False)
        iface.setActiveLayer(enregistre)
    else:
        iface.setActiveLayer(COUCHE)
    if objets_controle:
        QgsProject.instance().addMapLayer(couche_retraits_finale)
    iface.mapCanvas().refresh()
    print('\n=== CONTRÔLE PAR GROUPE / version {} ==='.format(PLUGIN_VERSION))
    print('Champ de regroupement :', partition_champ if partition_champ else 'ABSENT')
    print('Entrées :', len(source), '| Résultats :', dict(counts))
    print('Géométries réparées :', len(reparations), '| échecs :', len(echecs_reparation))
    print('Géométries modifiées :', len(modifs), '| relations traitées :', paires)
    print('Script intégré :', len(inclusions_script), 'inclusions supprimées ;', len(orientations_script), 'sens corrigés ;', len(nettoyages_finaux), 'nettoyages après découpe ;', len(anomalies_script), 'anomalies signalées.')
    print('DECOUPES :', len(objets_retraits), 'emprises de surfaces réellement retirées ;',
          round(surface_totale, 6), 'm² au total.')
    print('Intersections ambiguës non enregistrées comme découpes :', len(objets_ambigus))
    print('Recouvrements partiels : mode', 'examen manuel' if examen_avant_correction else 'découpe automatique validée')
    print('Entités ambiguës supprimées : 0 (option supprimée)')
    print('Doublons supprimés :', len(doublons_supprimes) + len(doublons_choisis),
          '(dont {} avec attributs divergents validés)'.format(len(doublons_choisis)))
    print('Groupes avec attributs divergents examinés :', len(doublons_douteux))
    print('Couche source traitée :', COUCHE.name())
    print('Couche de contrôle :', nom_controle)
    print('Sortie du contrôle :', (str(chemin_retraits) if mode_retraits == 'file' else 'mémoire temporaire') if objets_controle else 'aucune (état vide)')
    print('Groupes de partition :', len(groupes))
    print('Anomalies de calcul :', len(incidents))
    for ligne in incidents[:30]:
        print('AMBIGU :', ligne)
    if creer_couche_corrigee:
        print('Fichier créé et vérifié :', sortie)
    else:
        print('Aucune modification appliquée : aucune couche corrigée créée.')
    print('Données source inchangées.')
    _traitement_termine = True
    if not creer_couche_corrigee or not objets_controle:
        if not creer_couche_corrigee and not objets_controle:
            message = ('Aucune modification à appliquer.\n\n'
                       'Aucune couche corrigée ni couche d’état des corrections vide créée.\n'
                       'La couche source est conservée.')
        elif not creer_couche_corrigee:
            message = ('Aucune modification appliquée : aucune couche corrigée créée.\n'
                       'Les anomalies non corrigées restent signalées dans les statuts et le tableau.')
        else:
            message = ('La couche corrigée a été enregistrée.\n'
                       'Aucune surface n’a été retirée : aucune couche des découpes créée.')
        if echecs_reparation or incidents:
            message += ('\n\nDes anomalies restent signalées dans les rapports et la console ; '
                        'l’absence de modification ne signifie pas l’absence d’anomalie.')
        print(message)
        QMessageBox.information(iface.mainWindow(), versioned_title(tr('Bilan des corrections')), message)
except AnnulationUtilisateur as exc:
    print(str(exc))
finally:
    # Libérer les fournisseurs avant la suppression de la copie privée (Windows).
    dp = None
    controle = None
    if not _traitement_termine:
        enregistre = None
    if _dossier_travail is not None:
        try:
            _dossier_travail.cleanup()
        except OSError as exc:
            print('Nettoyage du dossier temporaire différé : {}'.format(exc))
    progression.close()
    # L’aperçu est remplacé par la couche corrigée définitive, ou supprimé
    # en cas d’annulation / erreur, sans laisser une couche trompeuse.
    if _apercu_id is not None and QgsProject.instance().mapLayer(_apercu_id):
        QgsProject.instance().removeMapLayer(_apercu_id)
    if not _traitement_termine and _noeud_source is not None:
        _noeud_source.setItemVisibilityChecked(_source_visible_initialement)
    iface.mapCanvas().refresh()
