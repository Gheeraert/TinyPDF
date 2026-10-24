# TinyPDF

Lecteur PDF léger pour Windows, conçu pour une seule tâche quotidienne :
**ouvrir un PDF, y apposer sa signature habituelle avec une ligne de texte, enregistrer.**

Fonctionne entièrement hors connexion : aucun compte, aucune télémétrie, aucun service distant.

## Utilisation

1. **Ouvrir** un PDF : `Ctrl+O`, glisser-déposer sur la fenêtre, ou double-clic si TinyPDF est
   l'application PDF par défaut.
2. **Signer** (`Ctrl+E`) : le bloc de signature suit la souris ; cliquez à l'endroit voulu.
3. Ajustez si besoin : glisser pour déplacer, carré bleu (coin) pour redimensionner,
   flèches du clavier pour un réglage fin (`Maj` = pas de 10 pt). `Entrée` valide, `Suppr` retire.
4. **Enregistrer** (`Ctrl+S`) : le nom `document_signe.pdf` est proposé ; `Entrée` suffit.

Au premier clic sur **Signer**, la fenêtre « Ma signature » s'ouvre : dessinez votre signature
(ou importez une image), réglez le texte, enregistrez. Elle est ensuite rechargée à chaque démarrage.

La flèche à côté de **Signer** donne accès à : *Signature manuscrite seule*, *Texte seul*,
*Modifier ma signature…*. La flèche à côté d'**Enregistrer** permet de *Remplacer le fichier
original* (avec confirmation).

### Texte de la signature

Ligne libre, par exemple `Lu et approuvé, le {date}`.

| Champ          | Remplacé par (exemple) |
|----------------|------------------------|
| `{date}`       | 24/10/2026             |
| `{date_longue}`| 24 octobre 2026        |

Police (toute police TrueType/OpenType installée), taille et couleur réglables. Disposition :
texte au-dessus de la signature, ou à sa gauche. Le texte est inséré comme du **vrai texte**
(police incorporée en sous-ensemble), sélectionnable et consultable.

### Raccourcis

| Action                       | Raccourci                         |
|------------------------------|-----------------------------------|
| Ouvrir / Enregistrer / Imprimer | `Ctrl+O` / `Ctrl+S` / `Ctrl+P` |
| Signer                       | `Ctrl+E`                          |
| Annuler la dernière signature | `Ctrl+Z`                         |
| Page suivante / précédente   | `Pg suiv` / `Pg préc` (ou `→` / `←`) |
| Première / dernière page     | `Début` / `Fin`                   |
| Zoom                         | `Ctrl+molette`, `Ctrl++`, `Ctrl+-` |
| Ajuster à la page / taille réelle / à la largeur | `Ctrl+0` / `Ctrl+1` / `Ctrl+2` |
| Fermer le document / Quitter | `Ctrl+W` / `Ctrl+Q`               |
| Annuler le placement en cours | `Échap`                          |

### Faire de TinyPDF le lecteur PDF par défaut

Clic droit sur un PDF → *Ouvrir avec* → *Choisir une autre application* → *Rechercher une autre
application sur ce PC* → sélectionner `TinyPDF.exe` → cocher *Toujours*.

## Installation

### Exécutable (recommandé)

Copier le dossier `dist\TinyPDF` où vous voulez (par exemple `C:\Programmes\TinyPDF`) et lancer
`TinyPDF.exe`. Aucune installation de Python n'est nécessaire.

### Depuis les sources

Python 3.10 ou plus récent :

```powershell
python -m pip install -r requirements.txt
python main.py [fichier.pdf]
```

### Construire l'exécutable

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1           # dossier dist\TinyPDF (démarrage rapide)
powershell -ExecutionPolicy Bypass -File build.ps1 -OneFile  # fichier unique dist\TinyPDF.exe
```

Le script crée un environnement isolé (`.venv-build`), installe les dépendances, **lance les
tests** (la construction s'arrête s'ils échouent) puis appelle PyInstaller via `TinyPDF.spec`.
La version « dossier » (~100 Mo) démarre plus vite que la version « fichier unique », qui doit
se décompresser à chaque lancement.

## Données personnelles

Dans `%APPDATA%\TinyPDF` :

- `signature.png` : signature manuscrite (PNG transparent, recadré) ;
- `settings.json` : texte, police, disposition, taille habituelle du bloc, zoom, dossiers récents ;
- `erreurs.log` : uniquement en cas d'erreur inattendue.

Pour repartir de zéro, supprimer ce dossier.

## Sécurité des documents

- Le PDF ouvert est lu en mémoire : le fichier n'est ni verrouillé ni modifié pendant la lecture.
- Les signatures ne sont intégrées qu'à l'enregistrement, sur une copie fraîche du document.
- **Enregistrement incrémental** : le fichier d'origine est recopié octet pour octet et les ajouts
  sont écrits à la suite. Contenu, polices, formulaires et structure sont donc intégralement
  préservés ; les pages ne sont jamais converties en images.
- L'écriture passe par un fichier temporaire, vérifié puis substitué atomiquement à la cible :
  un échec (disque plein, fichier ouvert ailleurs) ne laisse jamais de fichier à moitié écrit.
- Fermer avec des signatures non enregistrées demande confirmation.
- Pour les pièces jointes ouvertes depuis la messagerie (dossier temporaire), la copie signée
  est proposée dans le dernier dossier utilisé plutôt que dans le dossier temporaire.

### Cas particuliers

| Situation | Comportement |
|-----------|--------------|
| PDF protégé par mot de passe | Le mot de passe est demandé ; le fichier signé reste chiffré. |
| Modification interdite par l'auteur | Signature refusée avec un message explicite. |
| PDF signé électroniquement (certificat) | Avertissement ; l'ajout est enregistré en incrémental, la révision signée reste intacte (les lecteurs signaleront une modification postérieure). |
| Fichier ouvert dans un autre programme | Message clair, rien n'est écrasé. |
| Fichier endommagé ou non PDF | Message clair à l'ouverture. |

## Architecture

```
main.py                      point d'entrée
tinypdf/
  config.py                  dossier de données, préférences JSON (écriture atomique)
  fonts.py                   polices installées (registre Windows)
  signature.py               profil de signature, {date}, nettoyage d'image, persistance
  stamping.py                composition du bloc et intégration dans les pages
  document.py                ouverture, contrôles, rendu, enregistrement sûr
  app.py                     démarrage Qt, traduction, gestion des erreurs
  ui/
    main_window.py           fenêtre, barre d'outils, raccourcis, ouverture/enregistrement/impression
    pdf_view.py              vue continue, rendu paresseux, zoom, navigation, mode placement
    placement.py             bloc déplaçable et redimensionnable
    signature_dialog.py      zone de dessin, import d'image, réglages du texte, aperçu
    qt_convert.py, icon.py   utilitaires
tests/
  test_core.py               cœur PDF (sans interface)
  test_ui.py                 parcours complet dans l'interface (sans affichage)
```

Le cœur (`config`, `signature`, `stamping`, `document`) ne dépend pas de Qt.

**Choix techniques**

- **PySide6 + PyMuPDF**, comme proposé : PyMuPDF assure à la fois un rendu rapide et fidèle et
  l'écriture du PDF ; PySide6 fournit une interface native, l'impression et la gestion HiDPI.
- Le bloc de signature est composé comme une **mini-page PDF vectorielle** (texte réel + image
  avec masque de transparence), puis posé sur la page cible comme *Form XObject*. La même
  mini-page sert à l'aperçu à l'écran : ce que l'on voit est exactement ce qui est enregistré.
- Coordonnées : la vue travaille directement en points PDF dans le repère de la page affichée ;
  le zoom n'est qu'une transformation de la vue. La conversion vers le repère PDF tient compte
  de la rotation de la page et d'une éventuelle CropBox décalée (contournement d'une imprécision
  de PyMuPDF dans ce cas, couvert par les tests).
- Les polices Windows associent souvent l'espace à U+00A0 ; la table ToUnicode est corrigée
  pour que le texte inséré reste recherchable et copiable normalement.

## Tests

```powershell
python -m unittest discover -s tests
```

Couvrent : documents multipages, pages tournées (0/90/180/270°) avec ou sans CropBox, formats
mixtes et paysage, documents scannés, formulaires, PDF chiffrés, détection des signatures
électroniques, préservation de l'original, enregistrement incrémental, transparence de l'image,
et le parcours complet dans l'interface (clic à une position précise à différents niveaux de zoom).

## Licences

PyMuPDF est distribué sous licence **AGPL-3.0** (ou licence commerciale d'Artifex) ; PySide6
sous LGPL-3.0. Un usage personnel ne pose aucune difficulté ; en cas de redistribution de
l'exécutable, les obligations de l'AGPL s'appliquent (mise à disposition du code source).
