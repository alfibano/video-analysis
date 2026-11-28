[English](README.md) · [Français](README.fr.md) · [Español](README.es.md)

# Video Digest

**40 minutos de vídeo, 5 minutos de lectura.**<br>Lo esencial, estructurado y comentado, sin rodeos.

- Una ficha de estudio dividida en capítulos, con sus límites explicados.
- Fotogramas del vídeo junto al texto que ilustran.
- Traducción fiel al idioma solicitado.
- Recuadros con correcciones, complementos y preguntas, con fuentes numeradas.
- Referencias y resumen de los comentarios de los espectadores.
- Para estudiantes y personas con poco tiempo que quieren lo esencial sin verlo todo.

## Funciones

Cada ficha incluye un resumen adaptado a la duración del vídeo, capítulos con una breve explicación de sus límites y varios fotogramas distintos cuando aportan contexto. En el DOCX, cada imagen aparece junto al texto relacionado, en dos columnas.

El redactor conserva el sentido de lo dicho y lo traduce fielmente al idioma solicitado. Los recuadros distinguen correcciones, complementos útiles y preguntas esenciales. Las correcciones y los complementos citan referencias numeradas; las preguntas pueden no necesitar una fuente. La ficha termina con las referencias y resume los comentarios sin nombres ni identificadores.

Un bloque de datos presenta la información disponible del vídeo, como duración, fecha de publicación, visualizaciones e idiomas de los subtítulos, además del coste: uso medido para la corrección, estimación de tokens para la redacción y precios públicos de Anthropic con fecha. Los precios no disponibles aparecen como USD n/a.

## Instalación

Clona el repositorio y pon su carpeta de skill a disposición de tu agente:

```powershell
git clone https://github.com/alfibano/video-digest.git
# Windows: create a user-level Claude Code skill link
mklink /J "%USERPROFILE%\.claude\skills\video-digest" "<clone>\video-digest"
```

Para Codex u OpenClaw, instala o enlaza `video-digest/` como skill siguiendo el procedimiento de ese agente. La skill documenta el flujo de trabajo para Claude Code, Codex y OpenClaw.

```powershell
python video-digest/scripts/setup_tools.py
python video-digest/scripts/setup_tools.py --check
```

La configuración normal instala los paquetes que falten, proporciona ffmpeg si el sistema no lo tiene, guarda Whisper `small` en caché, prepara OCR y ejecuta el autotest sin conexión. `--check` solo ejecuta el autotest sin conexión. Las descargas de YouTube también necesitan Deno o Node. La configuración comprueba ffmpeg, yt-dlp y un entorno JavaScript, e instala las dependencias de Python que falten. Entorno probado: Windows 11 y Python 3.12.

## Analizar un vídeo

Con la skill instalada, pide a Claude Code, Codex u OpenClaw, en lenguaje sencillo:

> Haz una ficha de estudio de https://www.youtube.com/watch?v=VIDEO_ID

El agente ejecuta el análisis, prepara y redacta `sheet.json` en la sesión actual y después genera el documento Word editable. Los archivos de la ficha se guardan en `<video folder>/<video name>.analysis/`; el DOCX, en `<video name>.analysis/<video name>.docx`. Para vídeos locales, la skill pide permiso antes de enviar contenido a un servicio de IA remoto.

Estos apartados muestran los comandos manuales del proceso.

### URL

```powershell
python video-digest/scripts/analyze_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --dest "C:\Videos" --language en
```

La URL debe corresponder a un único vídeo que no sea en directo. No se aceptan listas de reproducción ni emisiones en directo o programadas.

### Archivo local

```powershell
python video-digest/scripts/analyze_video.py "C:\Videos\talk.mp4" --language fr
```

Los archivos locales se analizan en su ubicación. Usa `--language` para elegir el idioma de la transcripción y los subtítulos.

### Opciones

Estas son las opciones principales del comando. Los interruptores están desactivados por defecto, salvo que se indique lo contrario.

| Opción | Valor predeterminado y efecto |
|---|---|
| `--dest` | Directorio actual; para URL, carpeta de descarga. |
| `--sub-langs` | Si se omite, se amplía según el idioma del vídeo; solo para URL. |
| `--comments` | `100` comentarios principales para URL; rango `0–100`; `0` desactiva. |
| `--interval`, `--dedup-threshold` | `5` segundos entre fotogramas base; umbral `6` para diferencias entre fotogramas clave. |
| `--model`, `--transcribe`, `--device` | Whisper `small`; `auto` usa primero subtítulos (`whisper` fuerza el reconocimiento); `auto` prueba CUDA y, si falla, reinicia en CPU (también se puede elegir `cpu` o `cuda`). |
| `--correct-model`, `--host`, `--provider` | Ruta económica del proveedor o predeterminada; detecta Claude Code/Codex (OpenClaw debe indicarse); proveedor `auto` (también `claude-code`, `codex`, `openclaw`, `openai`, `local`). |
| `--retry-refused`, `--base-url`, `--ai-locality` | Reintentar segmentos rechazados (desactivado); `OPENAI_BASE_URL` o sin valor; ubicación según el proveedor, o remota si no se conoce. |
| `--allow-remote-ai`, `--no-correct` | Ambos desactivados; permitir corrección remota de contenido local o registrar que se omite intencionadamente la corrección. |
| `--language`, `--skip-frames`, `--contact-sheets`, `--skip-transcript` | Idioma `auto`; los demás interruptores desactivados. Elegir idioma, omitir fotogramas o Whisper, o crear hojas de fotogramas/contacto. |
| `--at`, `--fixed-interval` | Sin marca de tiempo; repetible para extraer fotogramas en resolución completa. Intervalo fijo desactivado; el muestreo adaptativo es el predeterminado. |
| `--motion-threshold`, `--motion-margin` | Umbral de cambio de escena `6`; margen de contexto de `1` segundo alrededor de los cambios detectados. |
| `--ocr-min-score`, `--skip-ocr` | Confianza mínima de OCR `0.5`; interruptor para omitirlo desactivado. |
| `--min-chapter`, `--max-chapter`, `--target-chapter` | `30`, `600` y `300` segundos para la duración mínima, máxima y objetivo de los capítulos de reserva. |

## Resultados

Todos los archivos de análisis y de la ficha se guardan junto al vídeo, en `<video folder>/<video name>.analysis/`. Si el vídeo se descarga desde una URL, queda junto a esa carpeta, no dentro de ella. Los scripts nunca borran los archivos generados.

| Archivo o carpeta | Contenido |
|---|---|
| `manifest.json` | Identidad del vídeo, ajustes y estado de las etapas/reanudación. |
| `metadata.json`, `raw_info.json`, `*.vtt` | Metadatos normalizados, datos de origen depurados y subtítulos disponibles o solicitados. |
| `comments.json`, `sponsorblock.json` | Comentarios opcionales de URL sin datos de identidad y marcas de patrocinio; nunca se recortan los segmentos patrocinados. |
| `frames/`, `frames.json`, `keyframes.txt` | Fotogramas muestreados con marcas de tiempo de referencia y lista de fotogramas clave (cambios en pantalla). |
| `sheets/`, `keysheets/` | Hojas de contacto 3×3 y páginas de fotogramas clave opcionales, solo con `--contact-sheets`. |
| `audio.wav` | Audio mono a 16 kHz, solo si se ejecuta Whisper. |
| `transcript.txt`, `transcript.json`, `transcript.corrected.json` | Voz con marcas de tiempo, de subtítulos o Whisper; la transcripción corregida opcional incluye la procedencia del proveedor/modelo. |
| `at/` | Fotogramas opcionales en resolución completa extraídos con `--at`. |
| `bundle.json` | Voz por capítulos, fotogramas con marcas de tiempo, segmentos patrocinados y OCR de fotogramas clave. |
| `sheet.brief.md`, `sheet.json` | Instrucciones de redacción y ficha de estudio escrita por el agente anfitrión. |
| `<video stem>.docx` | Ficha de estudio Word editable, que puede generarse con una plantilla. |

## Etapas del procesamiento

1. **Descargar** un vídeo de una URL y sus metadatos, subtítulos, comentarios y marcas de patrocinio; los archivos locales omiten esta etapa.
2. **Extraer** fotogramas muestreados y subtítulos utilizables. Whisper solo se ejecuta si no hay subtítulos o se selecciona `--transcribe whisper`.
3. **Corregir** los fragmentos seleccionados de la transcripción si hay un proveedor disponible; esta etapa se puede omitir.
4. **Crear el bundle** con capítulos, voz, fotogramas, segmentos patrocinados y texto OCR.
5. **Preparar la ficha** a partir del bundle y las imágenes seleccionadas.
6. **Escribir `sheet.json`** en un turno aparte con la sesión actual del agente anfitrión.
7. **Generar** el DOCX editable.

## Límites

| Configuración | Qué puedes esperar |
|---|---|
| Subtítulos utilizables | Se prefieren los subtítulos; Whisper no se ejecuta, así que la GPU no afecta a la transcripción. |
| Sin subtítulos, GPU NVIDIA y bibliotecas CUDA | Whisper usa CUDA `int8_float16`; si falla, se descarta el resultado parcial y la transcripción se reinicia en CPU `int8`. |
| Sin GPU NVIDIA | Whisper se ejecuta en CPU con `int8`. |

En el mismo vídeo de 43 minutos, v0.6.5 redujo el análisis automático de 2 091 a 518 segundos usando subtítulos. Sin subtítulos, Whisper tardó 273 segundos con una RTX 3050 Ti y CUDA, frente a unos 1 144 segundos en CPU. La carpeta de análisis pasó de 421 MB a 213 MB; las bibliotecas CUDA opcionales ocupan alrededor de 1 GB.

| Configuración de IA | Qué funciona |
|---|---|
| Anfitrión Claude Code o Codex | Puede corregir la transcripción y escribir la ficha con el modelo configurado en su sesión. |
| Anfitrión OpenClaw | Hay que seleccionarlo explícitamente; puede corregir la transcripción y escribir la ficha. La corrección en vivo no está verificada. |
| Ruta SDK de OpenAI o local | Solo corrige la transcripción; estas rutas SDK no escriben la ficha. |
| Sin anfitrión ni proveedor SDK | Tras la configuración, la extracción y `bundle.json` siguen funcionando sin conexión. Se omite la corrección y no está disponible la redacción automática de la ficha. |

## Confidencialidad

La corrección remota de la transcripción envía el texto transcrito y contexto limitado del vídeo, pero no el vídeo original, el audio, los fotogramas, el texto OCR ni la clave de API. La redacción remota de la ficha puede recibir el texto del bundle, metadatos, OCR y fotogramas seleccionados, pero no el vídeo ni el audio originales. Para contenido local, se pide consentimiento antes de procesarlo a distancia. Usa `--no-correct` para mantener la corrección sin conexión. Obtener un vídeo desde una URL es una acción de red independiente.

## Estado

v0.7.0 midió `DEFAULT_CHECKS=148` (148 filas, 0 fallidas) y `FULL_CHECKS=150` (150 superadas,
0 fallidas). Consulta las [notas de la versión](RELEASE_NOTES.md).

## Licencia

MIT — consulta [LICENSE](LICENSE).
