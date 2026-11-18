INTÉGRATION AUTOMATIQUE DU SCRIPT — 03/11/2026
Le plugin intègre maintenant controle_sens_polygones(1).py dans son propre
pipeline. Aucune exécution séparée du script, aucune nouvelle case à cocher.
1. Réparer/valider les géométries et nettoyer leurs résidus avant l'analyse.
2. Retirer les inclusions entièrement couvertes à attributs métier identiques.
3. Effectuer et stabiliser les découpes et doublons habituels ; le nettoyage
   et les inclusions sont réexaminés pendant les cycles internes.
4. Sur les résultats stabilisés : contours extérieurs horaires, trous
   antihoraires, sans modifier leur couverture. Vérification après export.
Les micro-parties <= 0,000001 m² sont retirées uniquement si le même objet
conserve un ensemble polygonal valide. Un micro-objet isolé n'est jamais vidé.
L'égalité des attributs est stricte, NULL différent de chaîne vide ; seuls
les identifiants techniques et résultats de contrôle sont exclus du test.
Tous les attributs source sont conservés. Les nouveaux champs de suivi courts
(sens_trace, sens_trous, geom_etat, geom_avant, reparation, sens_act) prennent
un suffixe numérique si leur nom existe déjà. Le détail des anneaux et des
résidus reste intégralement dans le CSV, sans champ long ajouté au SHP.
Les rapports DOUBLONS, REPARATIONS, diagnostics d'export et autres rapports
existants sont conservés. Ajouts conditionnels : SCRIPT.csv (suivi complet),
INCLUSIONS.csv (objets retirés et représentant final), SENS.csv (réorientations).
Une simple réorientation/réparation n'est jamais inscrite comme découpe.
Les emprises de découpes restent les surfaces réellement retirées.
Le nommage Correction plugin N et l'absence de sortie vide sont conservés.
Tests : six cas réels de géométries invalides/résidus ; commune 04123 réelle,
300 géométries valides après nettoyage/découpe/sens, nouvelle exécution après
écriture/relecture WKB sans modification ; rapports et textes longs vérifiés.
Ces validations portent sur le cœur GEOS et les blocs de rapports. L'exécution
complète avec l'interface et les fournisseurs QGIS reste à confirmer.
Version du plugin conservée.

--- Historique des évolutions précédentes ---

Correction géométrique des polygones — version 1.0.0
Auteur : François THEVAND <francois.thevand@gmail.com>

Installation : Extensions > Installer/Gérer les extensions > Installer depuis un ZIP.
Sélectionner Correction_Geometrique-1.0.0.zip.
Puis sélectionner une couche polygonale projetée dans QGIS et cliquer sur l'outil « Corriger les géométries des polygones » de la barre « Correction géométrique ».

Traitement de correction géométrique intégré au plugin. La couche originale n'est pas modifiée.
Compatibilité annoncée : QGIS 3.40 à QGIS 4.x. Vérification statique Python et des API Qt5/Qt6 effectuée ; essais réels QGIS 3 et 4 non disponibles dans cet environnement.

Correctif export SHP — 02/11/2026 (version conservée : 1.0.0)
- Extraction récursive des polygones issus des découpes GEOS ; retrait des
  composantes lignes/points sans modification des surfaces polygonales.
- Refus des résultats sans surface ou des multipolygones invalides.
- Même normalisation pour l'aperçu et les couches de contrôle.
- GeoPackage explicitement MultiPolygon ; contrôle du type WKB réel de
  toutes les entités après réouverture.
- Les anciennes sorties ne sont pas modifiées : relancer sur la source.
Validation : compilation Python ; 65 collections du GeoPackage fourni
normalisées via un adaptateur Shapely, avec égalité géométrique des surfaces.
Exécution complète dans QGIS non disponible dans l'environnement de test.

Destination commune — 02/11/2026
En mode Fichier physique, la couche PARTITIONS_CORRIGEES est livrée dans
le même dossier et le même format que CORRECTIONS_GEOMETRIQUES : SHP,
GeoPackage ou GeoJSON. Son nom horodaté évite d'écraser un résultat précédent.
Le GeoPackage de travail est temporaire et ne constitue plus la livraison.
L'export final est rouvert et comparé intégralement (nombre, attributs,
géométries polygonales). Le rapport des doublons est placé dans ce même dossier.
En mode mémoire du contrôle, la livraison corrigée reste un GeoPackage
près de la source. Version du plugin conservée : 1.0.0.

Correctif aperçu — 02/11/2026
- Une géométrie source non corrigée ne passe plus par le normaliseur strict
  dans l'aperçu ; les objets invalides restent conservés et signalés ambigus.
- Conversion explicite du résultat à une seule partie en MultiPolygon.
- Repli visuel sur l'original si une correction ne peut pas être normalisée ;
  la validation stricte avant enregistrement reste obligatoire.
Tests : aperçu d'une source invalide, correction valide, retour Polygon unique,
65 collections mixtes réelles et compilation Python. QGIS complet non exécuté.

Réparation automatique des géométries invalides — 02/11/2026
- Avant toute analyse : makeValid QGIS/GEOS puis extraction des seules surfaces
  polygonales, conversion MultiPolygon et validation. Aucun buffer approximatif.
- Attributs et source conservés ; les objets réparés participent aux calculs
  avec leurs nouvelles emprises dans l'index spatial.
- Champ correction = oui pour les réparations réussies. Les échecs restent
  signalés ambigu et ne sont pas supprimés silencieusement.
- Dans CORRECTIONS_GEOMETRIQUES, etat = repare identifie l'emprise réparée
  (bleu). Ce ne sont pas des surfaces retirées ; celles-ci restent etat = retire
  et sont calculées à partir de la base réparée, non de l'original invalide.
- Rapport *_REPARATIONS.csv : FID source, résultat, surfaces avant/après et motif
  d'échec. La surface d'un original invalide est informative, non une référence
  de conservation topologique.
- Livraison finale : comparaison topologique des géométries valides, compatible
  avec les changements d'ordre des anneaux et Polygon/MultiPolygon en SHP.
Validation : 15 géométries invalides de la source fournie, 15 réparables via
GEOS/Shapely ; tests du bloc d'inventaire, source inchangée, géométrie vide,
aperçu, cas polygonal unique et 65 collections mixtes. Compilation Python OK.
L'exécution complète dans QGIS reste à vérifier. Version conservée : 1.0.0.

Contrôle de livraison SHP — 02/11/2026
Le contrôle ne confond plus une représentation polygonale invalide issue de
la lecture SHP avec une perte de données : si nécessaire, seules les copies
utilisées pour la comparaison passent par makeValid/extraction polygonale,
puis GEOS doit confirmer l'égalité topologique exacte des surfaces. Aucune
marge surfacique n'est utilisée pour accepter une modification réelle.
Le fichier exporté n'est pas modifié par cette comparaison. Les cas où le
lecteur reconstitue des anneaux invalides sont explicitement listés dans
*_LECTURE_ANNEAUX.csv et restent à vérifier avec le lecteur SHP utilisé.
Les valeurs NULL réelles sont détectées par QgsVariantUtils.isNull ; la chaîne
littérale NULL reste une chaîne. Pour le SHP seulement : comparaison adaptée
aux champs DBF vides/NULL et espaces de remplissage à droite, sans troncature.
Un véritable échec écrit *_ERREUR_EXPORT.csv : FID source, champs avant/après,
valeurs attendues/obtenues ou diagnostic géométrique.
Validation GDAL réelle sur le GeoPackage fourni normalisé : 8363 objets écrits,
3 différences de regroupement des anneaux reproduites, surfaces identiques
après remise en validité ; contrôle corrigé réussi sur les 8363 objets.
Tests négatifs : perte de surface, déplacement et petite érosion refusés.
Compilation Python et tests précédents réussis. QGIS complet reste à vérifier.

Identifiants numériques SHP — 02/11/2026
Correction du faux échec fid_source 0.0 : OGR peut relire un LongLong DBF
largeur 20 en double. La comparaison utilise désormais Decimal pour les
valeurs numériques uniquement : 0 == 0.0, sans arrondi ni tolérance ; les
chaînes restent distinctes et conservent leurs zéros initiaux.
Le même traitement est utilisé pour rechercher les références et produire
les diagnostics, évitant une fausse absence de correspondance bijective.
L'export principal SHP utilise RESIZE=YES pour adapter les largeurs DBF.
Validation : reproduction GDAL réelle du champ N(20,0) relu en float 0.0,
comparaison 0/0.0, index de correspondance, rejet de fractions non nulles,
préservation de 04004 et détection de perte de précision au-delà de 2^53.
Compilation Python réussie ; exécution complète QGIS restant à vérifier.

Absence de corrections — 02/11/2026
Sans modification géométrique, réparation ni suppression de
doublon validée : aucune couche corrigée n'est exportée ou ajoutée au projet.
La copie privée de travail est supprimée à la fin. La source reste active
et sa visibilité est conservée. Aucun rapport de doublons vide n'est créé.
Un état de contrôle vide n'est ni exporté ni ajouté au projet. Une boîte
Bilan des corrections remplace la sortie vide et précise les couches non créées.
Les anomalies ou ambiguïtés non corrigées restent signalées, sans les présenter
comme une absence d'anomalie ; leur couche de contrôle non vide est conservée.
Tests des blocs Python réels : absence d'export final, quatre combinaisons
corrigée/contrôle, activation de la source et messages. Compilation Python OK.
Version conservée : 1.0.0. Exécution complète dans QGIS à vérifier.

Emprises des découpes uniquement — 02/11/2026
Modification fondée sur l'archive jointe Correction_Geometrique_sans_sorties_inutiles.zip,
sans reprendre la séparation ultérieure en trois couches indépendantes.
La couche de contrôle affichée DECOUPES contient exclusivement les surfaces
réellement retirées (trous intérieurs et découpes en bordure), calculées par
la différence entre la géométrie de référence et le résultat enregistré.
Si l'original était invalide, la géométrie réparée sert de référence pour
isoler la découpe de la réparation topologique.
Aucune emprise entière de polygone réparé n'y est ajoutée. Les
intersections ambiguës non découpées n'y sont pas enregistrées comme trous ;
elles restent consultables dans le tableau et les statuts du résultat principal.
Les réparations et découpes restent cumulées dans la
couche principale PARTITIONS_CORRIGEES, comme dans la version jointe.
Aucune couche de découpes n'est exportée/ajoutée lorsqu'aucune surface n'a
été retirée ; un message d'information le précise. Source inchangée.
Contrôles : compilation Python ; bloc de calcul exécuté avec géométries
rectangulaires simulées (trou intérieur, retrait en bordure, réparation seule,
inversion seule), FID et surfaces ; exclusion des objets complets et ambiguïtés.
Exécution complète dans QGIS restant à vérifier. Version conservée : 1.0.0.

Suppression du traitement du sens des polygones
La case à cocher, les réorientations explicites, le rapport SENS et le contrôle
du sens à l’export sont supprimés. Les contrôles attributaires et topologiques
restent actifs. Nommage Correction plugin N conservé.

Doublons géométriques — 03/11/2026
La détection utilise désormais l’égalité topologique GEOS, sans tampon ni
arrondi, et la comparaison des attributs réglementaires. Elle reconnaît les
contours identiques malgré un ordre différent des sommets, trous ou parties.
Les attributs divergents restent soumis au choix utilisateur.
Une seconde détection après les découpes retire les doublons parfaits créés
par le traitement lui-même, dès le premier passage.
Tests GEOS ciblés réussis ; exécution complète QGIS restant à vérifier.

Stabilisation dans une seule exécution — 03/11/2026
Les choix sur les doublons à attributs divergents sont présentés avant
l’analyse des découpes. Les copies exclues ne servent plus de masques.
Après les découpes, le résultat est réexaminé par cycles internes : doublons,
inclusions et recouvrements traitables de la même partition, jusqu’à stabilité.
L’index spatial limite les recherches ; seules les zones modifiées sont
réexaminées après le premier cycle. Les décisions non cochées sont respectées.
Les nouveaux conflits nécessitant un choix sont présentés dans la même
exécution. Sans partition, seuls les inclusions et doublons sont automatiques.
Les ambiguïtés résolues et références à des doublons supprimés sont nettoyées.
Une limite de 100 cycles interrompt la livraison si la stabilité n’est pas obtenue.
Aucun automatisme ne vide une géométrie ni ne supprime un attribut divergent.
Tests GEOS : commune avec doublons parfaits, inclusions, recouvrements,
second traitement inchangé, refus manuels et séparation des partitions.
Syntaxe vérifiée ; exécution complète QGIS et commune réelle à confirmer.

Validation sur la commune 04123 fournie — 03/11/2026
Entrée : N_ZONE_URBA_004_correction script 1.gpkg. Comparaison des sorties
plugin 8 et 9 : 14 géométries modifiées, tous leurs attributs conservés.
Les copies de cette commune sont quasi identiques, avec de fines différences
de coordonnées ; par exemple contrôle_pk 2038 / 3876 : environ 0,685 m² de
différence symétrique, seuls les identifiants techniques diffèrent.
Les opérations GEOS utilisent désormais un modèle de précision commun
de 0,000001 m (un micromètre) pour intersections, différences et unions.
Aucun buffer n’est utilisé. Les seuls résultats des opérations sont calculés
sur cette grille ; la source reste inchangée. Les contrôles surfaciques
prennent en compte la borne de déplacement numérique grille × périmètre.
La priorité automatique est recalculée sur les géométries courantes à chaque
cycle. Les choix manuels non cochés restent exclus. Une passe globale finale
remplace l’ancien arrêt possible sur un voisinage seulement partiellement revu.
Le champ technique controle_pk est exclu de la comparaison des attributs
réglementaires pour les doublons, mais reste présent avec ses valeurs dans
la sortie. Les autres attributs sont conservés et contrôlés comme auparavant.
Tests du cœur GEOS en mode automatique sur les 300 objets réels de 04123 :
29 cycles internes, aucune erreur ; 300 géométries valides sur 300.
Après sérialisation/relecture WKB, une nouvelle exécution : 0 modification,
0 incident. Ce test porte sur le cœur géométrique ; l’exécution complète
de l’interface et des fournisseurs QGIS reste à valider sur le poste utilisateur.
