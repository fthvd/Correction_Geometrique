# -*- coding: utf-8 -*-
"""Corrections du script intégrées au pipeline, sans interface ni export séparé."""
import math
import re
from decimal import Decimal, localcontext
from qgis.core import Qgis, QgsGeometry, QgsWkbTypes, QgsSpatialIndex, QgsFeature, QgsVariantUtils

SEUIL_RESIDU_M2 = 0.000001


def noms_attributs_metier(noms):
    techniques = {'fid', 'id', 'gid', 'ogc_fid', 'controle_pk', 'fid_source',
        'correction', 'sens_trace', 'sens_trous', 'geom_etat', 'sens_detail',
        'geom_avant', 'reparation', 'rep_detail', 'sens_action', 'sens_info',
        'obj_absorbes', 'sens_act', 'nb_absorb'}
    return [nom for nom in noms
            if re.sub(r'_\d+$', '', nom.casefold()) not in techniques]


def sens_points(points):
    if len(points) < 4:
        raise ValueError('Anneau incomplet')
    x0, y0 = points[0].x(), points[0].y()
    termes = [(p.x() - x0) * (points[(i + 1) % len(points)].y() - y0)
              - (points[(i + 1) % len(points)].x() - x0) * (p.y() - y0)
              for i, p in enumerate(points)]
    aire = math.fsum(termes)
    if abs(aire) <= math.fsum(abs(t) for t in termes) * 1e-12:
        with localcontext() as ctx:
            ctx.prec = 160
            origine_x = Decimal.from_float(float(x0))
            origine_y = Decimal.from_float(float(y0))
            aire = Decimal(0)
            for i, p in enumerate(points):
                q = points[(i + 1) % len(points)]
                x, y = Decimal.from_float(float(p.x())) - origine_x, Decimal.from_float(float(p.y())) - origine_y
                xx, yy = Decimal.from_float(float(q.x())) - origine_x, Decimal.from_float(float(q.y())) - origine_y
                aire += x * yy - xx * y
    if not aire:
        raise ValueError('Anneau dégénéré : sens indéterminé')
    return 'ANTIHORAIRE' if aire > 0 else 'HORAIRE'


def sens_anneau(anneau):
    if anneau is None or not anneau.isClosed():
        raise ValueError('Anneau absent ou non fermé')
    return sens_points(anneau.points())

def composantes(geom):
    if geom.isMultipart() or QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.GeometryCollection:
        resultat = []
        for partie in geom.asGeometryCollection():
            resultat.extend(composantes(partie))
        return resultat
    return [QgsGeometry(geom)]

def retirer_residus(geom, seuil):
    polygones, residus = [], []
    for partie in composantes(geom):
        if (not partie.isNull() and not partie.isEmpty()
                and QgsWkbTypes.geometryType(partie.wkbType()) == QgsWkbTypes.PolygonGeometry
                and (seuil <= 0 or partie.area() > seuil)):
            polygones.append(partie)
        else:
            residus.append(partie)
    if not residus:
        return geom, ''
    if not polygones or not all(p.isGeosValid() for p in polygones):
        raise ValueError('Résidus conservés : aucun ensemble de contours valides dans le même objet')
    resultat = QgsGeometry.collectGeometry(polygones)
    if resultat.isNull() or resultat.isEmpty() or not resultat.isGeosValid():
        raise ValueError('Résidus conservés : ensemble polygonal restant invalide')
    detail = 'Résidus du même objet retirés (attributs communs) : ' + ' | '.join(
        '{} ; aire={:.12g} unités² ; WKT={}'.format(
            QgsWkbTypes.displayString(p.wkbType()), p.area(), p.asWkt(17)) for p in residus)
    return resultat, detail

def cle_attributs(objet, indices):
    # Valeurs exactes : NULL reste différent d'une chaîne vide ; aucun trim.
    return tuple(None if objet[i] is None or QgsVariantUtils.isNull(objet[i])
                 else (type(objet[i]).__name__, repr(objet[i])) for i in indices)

def resume_sens(valeurs, vide):
    distincts = set(valeurs)
    if not distincts:
        return vide
    return next(iter(distincts)) if len(distincts) == 1 else 'MIXTE'

def controle(geom):
    if geom.isNull() or geom.isEmpty():
        return ['VIDE', 'VIDE', 'VIDE', 'Géométrie absente ou vide']
    if not geom.isGeosValid():
        return ['INDETERMINE', 'INDETERMINE', 'INVALIDE',
                'Géométrie invalide : sens non certifié']
    exterieurs, trous, details = [], [], []
    for numero, partie in enumerate(geom.constParts(), 1):
        if not hasattr(partie, 'exteriorRing'):
            raise ValueError('Partie non polygonale')
        sens = sens_anneau(partie.exteriorRing())
        exterieurs.append(sens)
        interieurs = [sens_anneau(partie.interiorRing(i))
                      for i in range(partie.numInteriorRings())]
        trous.extend(interieurs)
        details.append('Partie {} : extérieur={} ; trous={}'.format(
            numero, sens, ', '.join('{}:{}'.format(i, s)
                                  for i, s in enumerate(interieurs, 1))
            or 'aucun'))
    if not exterieurs:
        raise ValueError('Aucun contour extérieur')
    return [resume_sens(exterieurs, 'INDETERMINE'),
            resume_sens(trous, 'SANS_TROU'), 'VALIDE', ' | '.join(details)]

def reparer(geom, seuil=0):
    """Répare puis retire les résidus si le même objet garde une surface valide."""
    original = QgsGeometry(geom)
    if geom.isNull() or geom.isEmpty():
        return original, 'VIDE', 'IMPOSSIBLE', 'Géométrie vide conservée'
    valide = geom.isGeosValid()
    avant = 'VALIDE' if valide else 'INVALIDE'
    try:
        if not valide and QgsWkbTypes.hasM(geom.wkbType()):
            raise ValueError('Réparation non appliquée pour préserver les mesures M')
        candidate = QgsGeometry(geom) if valide else geom.makeValid()
        if candidate.isNull() or candidate.isEmpty():
            raise ValueError(candidate.lastError() or 'Résultat vide')
        corrigee, detail = retirer_residus(candidate, seuil)
        if QgsWkbTypes.geometryType(corrigee.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise ValueError('Résultat non polygonal')
        for dim, test in (('Z', QgsWkbTypes.hasZ), ('M', QgsWkbTypes.hasM)):
            if test(geom.wkbType()) and not test(corrigee.wkbType()):
                raise ValueError('Perte de ' + dim + ' détectée ; original conservé')
        if not corrigee.isGeosValid():
            raise ValueError('Géométrie toujours invalide après réparation')
        statut = 'RESIDUS_SUPPRIMES' if valide and detail else (
            'NON_NECESSAIRE' if valide else 'REPAREE')
        return corrigee, avant, statut, detail or ('' if valide else 'Réparation makeValid validée')
    except Exception as erreur:
        return original, avant, 'ECHEC', str(erreur)

def orienter(geom):
    if geom.isNull() or geom.isEmpty() or not geom.isGeosValid():
        return geom, 'NON_APPLICABLE', 'Géométrie vide ou invalide'
    try:
        avant = controle(geom)
        if avant[0] == 'HORAIRE' and avant[1] in ('ANTIHORAIRE', 'SANS_TROU'):
            return geom, 'DEJA_CONFORME', ''
        polygones = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
        for polygone in polygones:
            for i, anneau in enumerate(polygone):
                attendu = 'HORAIRE' if i == 0 else 'ANTIHORAIRE'
                if sens_points(anneau) != attendu:
                    polygone[i] = list(reversed(anneau))
        corrigee = QgsGeometry.fromMultiPolygonXY(polygones) if geom.isMultipart() else QgsGeometry.fromPolygonXY(polygones[0])
        if corrigee.isNull() or corrigee.isEmpty() or not corrigee.isGeosValid() or not corrigee.isGeosEqual(geom):
            raise ValueError('Réorientation sans conservation topologique')
        apres = controle(corrigee)
        if apres[0] != 'HORAIRE' or apres[1] not in ('ANTIHORAIRE', 'SANS_TROU'):
            raise ValueError('Sens attendu non obtenu pour tous les anneaux')
        return corrigee, 'CORRIGE', 'Extérieurs horaires ; trous antihoraires'
    except Exception as erreur:
        return geom, 'ECHEC', str(erreur)


def trouver_inclusions(geometries, attributs, noms, suivi=None):
    """Couverture entière exacte + attributs strictement identiques, sans grille."""
    if not noms:
        return {}
    index = QgsSpatialIndex()
    cles = {}
    for fid, geom in geometries.items():
        if geom.isNull() or geom.isEmpty() or not geom.isGeosValid():
            continue
        f = QgsFeature()
        f.setId(fid)
        f.setGeometry(geom)
        index.addFeature(f)
        cles[fid] = cle_attributs(attributs[fid], noms)
    ordre = sorted(cles, key=lambda fid: (-geometries[fid].area(), fid))
    rang = {fid: i for i, fid in enumerate(ordre)}
    retires = {}
    for numero, fid in enumerate(ordre, 1):
        if suivi and (numero == 1 or numero % 25 == 0 or numero == len(ordre)):
            suivi(numero, len(ordre), fid)
        g = geometries[fid]
        for parent in sorted(index.intersects(g.boundingBox()), key=lambda x: rang[x]):
            if rang[parent] >= rang[fid] or parent in retires or cles[parent] != cles[fid]:
                continue
            contenant = geometries[parent]
            # contains inclut les frontières communes pour des polygones.
            # La différence vérifie une couverture sans tolérance surfacique.
            if g.isGeosEqual(contenant):
                retires[fid] = parent
                break
            if not contenant.contains(g):
                continue
            reste = g.difference(contenant)
            if not reste.isNull() and not reste.lastError() and reste.isEmpty():
                retires[fid] = parent
                break
    return retires
