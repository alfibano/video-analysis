[English](README.md) · [Français](README.fr.md) · [Español](README.es.md)

# Video Digest

**40 minutes de vidéo, 5 minutes de lecture.**<br>L'essentiel, structuré et commenté, sans les digressions.

- Une fiche d'étude organisée en chapitres, avec leurs limites expliquées.
- Des images de la vidéo placées à côté du texte qu'elles illustrent.
- Une traduction fidèle dans la langue demandée.
- Des encadrés pour les corrections, les compléments et les questions, avec des sources numérotées.
- Une liste de références et un résumé des commentaires des spectateurs.
- Pour les étudiants et les personnes pressées qui veulent l'essentiel sans tout regarder.

## Fonctionnalités

Chaque fiche comprend un résumé adapté à la durée de la vidéo, des chapitres dont les limites sont brièvement justifiées et des images distinctes lorsqu'elles apportent du contexte. Dans le DOCX, chaque image est placée à côté du texte correspondant, sur deux colonnes.

La rédaction respecte le sens des propos et les traduit fidèlement dans la langue demandée. Les encadrés distinguent corrections, compléments utiles et questions essentielles. Corrections et compléments renvoient à des références numérotées ; les questions peuvent ne pas avoir de source. La fiche se termine par les références et résume les commentaires sans nom ni identifiant.

Un bloc récapitule les informations disponibles sur la vidéo : durée, date de publication, vues et langues des sous-titres. Il indique aussi les coûts : usage mesuré pour la correction, estimation des jetons pour la rédaction, et tarifs publics d'Anthropic datés ; les prix non disponibles sont indiqués « USD n/a ».

## Installation

Clonez le dépôt et mettez son dossier de compétence à disposition de votre agent :

```powershell
git clone https://github.com/alfibano/video-digest.git
# Windows : créer un lien de compétence Claude Code pour l'utilisateur
mklink /J "%USERPROFILE%\.claude\skills\video-digest" "<clone>\video-digest"
```

Pour Codex ou OpenClaw, installez ou reliez `video-digest/` comme compétence selon la procédure de cet agent. La compétence décrit le flux de travail pour Claude Code, Codex et OpenClaw.

```powershell
python video-digest/scripts/setup_tools.py
python video-digest/scripts/setup_tools.py --check
```

La configuration normale installe les dépendances manquantes, fournit ffmpeg si le système ne l'a pas, met en cache Whisper `small`, initialise l'OCR et lance l'auto-test hors ligne. `--check` lance uniquement cet auto-test. Le téléchargement depuis YouTube nécessite aussi Deno ou Node. La configuration vérifie ffmpeg, yt-dlp et un environnement JavaScript, puis installe les dépendances Python manquantes. Environnement testé : Windows 11 et Python 3.12.

## Analyser une vidéo

Une fois la compétence installée, demandez simplement à Claude Code, Codex ou OpenClaw :

> Fais une fiche d'étude de https://www.youtube.com/watch?v=VIDEO_ID

L'agent lance l'analyse, prépare et rédige `sheet.json` dans la session en cours, puis génère le document Word modifiable. Les fichiers sont enregistrés dans `<video folder>/<video name>.analysis/` ; le DOCX se trouve dans `<video name>.analysis/<video name>.docx`. Pour une vidéo locale, la compétence demande votre accord avant tout envoi à un service d'IA distant.

Les exemples suivants montrent les commandes manuelles du pipeline.

### URL

```powershell
python video-digest/scripts/analyze_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --dest "C:\Videos" --language en
```

L'URL doit pointer vers une seule vidéo qui n'est pas en direct. Les playlists et les diffusions en direct ou à venir sont refusées.

### Fichier local

```powershell
python video-digest/scripts/analyze_video.py "C:\Videos\talk.mp4" --language fr
```

Les fichiers locaux sont analysés sur place. `--language` définit la langue de la transcription et des sous-titres.

### Options

Les options principales sont détaillées ci-dessous. Sauf indication contraire, les options à bascule sont désactivées.

| Option | Valeur par défaut et effet |
|---|---|
| `--dest` | Dossier courant ; pour les URL uniquement, dossier de téléchargement. |
| `--sub-langs` | Si omis, déduit les langues de la vidéo ; URL uniquement. |
| `--comments` | `100` commentaires URL ; plage `0–100` ; `0` désactive. |
| `--interval`, `--dedup-threshold` | `5` secondes entre les images de base ; seuil `6` de différence pour les images clés. |
| `--model`, `--transcribe`, `--device` | Whisper `small` ; `auto` privilégie les sous-titres (`whisper` force la transcription) ; `auto` essaie CUDA puis redémarre sur CPU (`cpu` ou `cuda` possibles aussi). |
| `--correct-model`, `--host`, `--provider` | Modèle économique du fournisseur ou valeur par défaut ; détecte Claude Code/Codex (OpenClaw doit être indiqué) ; fournisseur `auto` (`claude-code`, `codex`, `openclaw`, `openai`, `local` aussi). |
| `--retry-refused`, `--base-url`, `--ai-locality` | Réessayer les segments refusés (désactivé) ; `OPENAI_BASE_URL` ou non défini ; localisation déduite du fournisseur, sinon distante. |
| `--allow-remote-ai`, `--no-correct` | Désactivés ; autoriser la correction distante d'un média local ou enregistrer le choix de l'ignorer. |
| `--language`, `--skip-frames`, `--contact-sheets`, `--skip-transcript` | Langue `auto` ; autres options désactivées. Choisir la langue, omettre les images ou Whisper, ou créer des planches d'images. |
| `--at`, `--fixed-interval` | Aucun horodatage ; répétable pour extraire des images en pleine résolution. Intervalle fixe désactivé ; échantillonnage adaptatif par défaut. |
| `--motion-threshold`, `--motion-margin` | Seuil de changement de scène `6` ; marge de contexte d'`1` seconde autour des changements détectés. |
| `--ocr-min-score`, `--skip-ocr` | Confiance OCR minimale `0.5` ; désactivé. |
| `--min-chapter`, `--max-chapter`, `--target-chapter` | `30`, `600` et `300` secondes pour les durées minimale, maximale et cible des chapitres de secours. |

## Résultats

Tous les fichiers d'analyse et de fiche sont enregistrés à côté de la vidéo dans `<video folder>/<video name>.analysis/`. Une vidéo téléchargée depuis une URL est enregistrée à côté de ce dossier, pas dedans. Les scripts ne suppriment jamais les fichiers produits.

| Fichier ou dossier | Contenu |
|---|---|
| `manifest.json` | Identité de la vidéo, réglages et état des étapes/reprise. |
| `metadata.json`, `raw_info.json`, `*.vtt` | Métadonnées normalisées, données source assainies et sous-titres disponibles ou demandés. |
| `comments.json`, `sponsorblock.json` | Commentaires URL sans identité et segments SponsorBlock (facultatifs) ; les passages sponsorisés ne sont jamais coupés. |
| `frames/`, `frames.json`, `keyframes.txt` | Images échantillonnées avec horodatages de référence et liste des images clés (changements à l'écran). |
| `sheets/`, `keysheets/` | Planches de contact 3×3 et pages d'images clés (facultatives, avec `--contact-sheets`). |
| `audio.wav` | Audio mono à 16 kHz, uniquement si Whisper est lancé. |
| `transcript.txt`, `transcript.json`, `transcript.corrected.json` | Paroles horodatées issues des sous-titres ou de Whisper ; transcription corrigée facultative avec provenance du fournisseur/modèle. |
| `at/` | Images facultatives en pleine résolution extraites avec `--at`. |
| `bundle.json` | Chapitres, paroles, images horodatées, segments sponsorisés et OCR des images clés. |
| `sheet.brief.md`, `sheet.json` | Consignes de rédaction et fiche d'étude rédigée par l'agent hôte. |
| `<video stem>.docx` | Fiche Word modifiable, éventuellement générée à partir d'un modèle. |

## Étapes de traitement

1. **Récupération** d'une vidéo URL, de ses métadonnées, sous-titres, commentaires et segments sponsorisés ; les fichiers locaux passent cette étape.
2. **Extraction** des images échantillonnées et des sous-titres utilisables. Whisper ne se lance qu'en l'absence de sous-titres ou avec `--transcribe whisper`.
3. **Correction** éventuelle des passages sélectionnés si un fournisseur est disponible ; cette étape peut être ignorée.
4. **Création du bundle** avec chapitres, paroles, images, segments sponsorisés et texte OCR.
5. **Préparation de la fiche** à partir du bundle et des images sélectionnées.
6. **Rédaction de `sheet.json`** lors d'un tour distinct avec la session de l'agent hôte.
7. **Génération** du DOCX modifiable.

## Limites

| Configuration | Résultat attendu |
|---|---|
| Sous-titres utilisables | Ils sont privilégiés ; Whisper ne tourne pas, donc le GPU ne change rien à la transcription. |
| Pas de sous-titres, GPU NVIDIA et bibliothèques CUDA | Whisper utilise CUDA `int8_float16` ; en cas d'échec, la sortie partielle est supprimée et la transcription repart sur CPU `int8`. |
| Pas de GPU NVIDIA | Whisper tourne sur CPU avec `int8`. |

Sur la même vidéo de 43 minutes, v0.6.5 a ramené l'analyse automatique de 2 091 à 518 secondes avec les sous-titres. Sans sous-titres, Whisper a pris 273 secondes sur RTX 3050 Ti avec CUDA, contre environ 1 144 secondes sur CPU. Le dossier d'analyse est passé de 421 à 213 Mo ; les bibliothèques CUDA facultatives occupent environ 1 Go.

| Configuration IA | Fonctionnement |
|---|---|
| Hôte Claude Code ou Codex | L'hôte peut corriger la transcription et rédiger la fiche avec le modèle de sa session. |
| Hôte OpenClaw | À sélectionner explicitement ; il peut corriger la transcription et rédiger la fiche. La correction en direct n'est pas vérifiée. |
| Route SDK OpenAI ou locale | Correction de transcription uniquement ; ces routes ne rédigent pas la fiche. |
| Aucun hôte ni fournisseur SDK | Après configuration, l'extraction et `bundle.json` restent disponibles hors ligne. La correction est ignorée et la rédaction automatique de la fiche est indisponible. |

## Confidentialité

La correction distante envoie le texte transcrit et un contexte vidéo limité, pas la vidéo, l'audio, les images, le texte OCR ni la clé API. La rédaction distante de la fiche peut recevoir le texte du bundle, les métadonnées, l'OCR et certaines images, mais ni la vidéo ni l'audio bruts. Pour un média local, votre accord est demandé avant tout traitement distant. `--no-correct` garde la correction hors ligne. La récupération d'une URL reste une action réseau distincte.

## Statut

v0.6.5 a mesuré `DEFAULT_CHECKS=145` (145 réussis, 0 échec) et `FULL_CHECKS=147` (147 réussis, 0 échec). Voir les [notes de version](RELEASE_NOTES.md).

## Licence

MIT — voir [LICENSE](LICENSE).
