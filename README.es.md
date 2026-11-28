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

<table><thead><tr><th>Opción</th><th>Valor predeterminado y efecto</th></tr></thead><tbody><tr><td><code>--dest</code></td><td>Directorio actual; para URL, carpeta de descarga.</td></tr><tr><td><code>--sub-langs</code></td><td>Si se omite, se amplía según el idioma del vídeo; solo para URL.</td></tr><tr><td><code>--comments</code></td><td><code>100</code> comentarios principales para URL; rango <code>0–100</code>; <code>0</code> desactiva.</td></tr><tr><td><code>--interval</code>, <code>--dedup-threshold</code></td><td><code>5</code> segundos entre fotogramas base; umbral <code>6</code> para diferencias entre fotogramas clave.</td></tr><tr><td><code>--model</code>, <code>--transcribe</code>, <code>--device</code></td><td>Whisper <code>small</code>; <code>auto</code> usa primero subtítulos (<code>whisper</code> fuerza el reconocimiento); <code>auto</code> prueba CUDA y, si falla, reinicia en CPU (también se puede elegir <code>cpu</code> o <code>cuda</code>).</td></tr><tr><td><code>--correct-model</code>, <code>--host</code>, <code>--provider</code></td><td>Ruta económica del proveedor o predeterminada; detecta Claude Code/Codex (OpenClaw debe indicarse); proveedor <code>auto</code> (también <code>claude-code</code>, <code>codex</code>, <code>openclaw</code>, <code>openai</code>, <code>local</code>).</td></tr><tr><td><code>--retry-refused</code>, <code>--base-url</code>, <code>--ai-locality</code></td><td>Reintentar segmentos rechazados (desactivado); <code>OPENAI_BASE_URL</code> o sin valor; ubicación según el proveedor, o remota si no se conoce.</td></tr><tr><td><code>--allow-remote-ai</code>, <code>--no-correct</code></td><td>Ambos desactivados; permitir corrección remota de contenido local o registrar que se omite intencionadamente la corrección.</td></tr><tr><td><code>--language</code>, <code>--skip-frames</code>, <code>--contact-sheets</code>, <code>--skip-transcript</code></td><td>Idioma <code>auto</code>; los demás interruptores desactivados. Elegir idioma, omitir fotogramas o Whisper, o crear hojas de fotogramas/contacto.</td></tr><tr><td><code>--at</code>, <code>--fixed-interval</code></td><td>Sin marca de tiempo; repetible para extraer fotogramas en resolución completa. Intervalo fijo desactivado; el muestreo adaptativo es el predeterminado.</td></tr><tr><td><code>--motion-threshold</code>, <code>--motion-margin</code></td><td>Umbral de cambio de escena <code>6</code>; margen de contexto de <code>1</code> segundo alrededor de los cambios detectados.</td></tr><tr><td><code>--ocr-min-score</code>, <code>--skip-ocr</code></td><td>Confianza mínima de OCR <code>0.5</code>; interruptor para omitirlo desactivado.</td></tr><tr><td><code>--min-chapter</code>, <code>--max-chapter</code>, <code>--target-chapter</code></td><td><code>30</code>, <code>600</code> y <code>300</code> segundos para la duración mínima, máxima y objetivo de los capítulos de reserva.</td></tr></tbody></table>

## Resultados

Todos los archivos de análisis y de la ficha se guardan junto al vídeo, en `<video folder>/<video name>.analysis/`. Si el vídeo se descarga desde una URL, queda junto a esa carpeta, no dentro de ella. Los scripts nunca borran los archivos generados.

<table><thead><tr><th>Archivo o carpeta</th><th>Contenido</th></tr></thead><tbody><tr><td><code>manifest.json</code></td><td>Identidad del vídeo, ajustes y estado de las etapas/reanudación.</td></tr><tr><td><code>metadata.json</code>, <code>raw_info.json</code>, <code>*.vtt</code></td><td>Metadatos normalizados, datos de origen depurados y subtítulos disponibles o solicitados.</td></tr><tr><td><code>comments.json</code>, <code>sponsorblock.json</code></td><td>Comentarios opcionales de URL sin datos de identidad y marcas de patrocinio; nunca se recortan los segmentos patrocinados.</td></tr><tr><td><code>frames/</code>, <code>frames.json</code>, <code>keyframes.txt</code></td><td>Fotogramas muestreados con marcas de tiempo de referencia y lista de fotogramas clave (cambios en pantalla).</td></tr><tr><td><code>sheets/</code>, <code>keysheets/</code></td><td>Hojas de contacto 3×3 y páginas de fotogramas clave opcionales, solo con <code>--contact-sheets</code>.</td></tr><tr><td><code>audio.wav</code></td><td>Audio mono a 16 kHz, solo si se ejecuta Whisper.</td></tr><tr><td><code>transcript.txt</code>, <code>transcript.json</code>, <code>transcript.corrected.json</code></td><td>Voz con marcas de tiempo, de subtítulos o Whisper; la transcripción corregida opcional incluye la procedencia del proveedor/modelo.</td></tr><tr><td><code>at/</code></td><td>Fotogramas opcionales en resolución completa extraídos con <code>--at</code>.</td></tr><tr><td><code>bundle.json</code></td><td>Voz por capítulos, fotogramas con marcas de tiempo, segmentos patrocinados y OCR de fotogramas clave.</td></tr><tr><td><code>sheet.brief.md</code>, <code>sheet.json</code></td><td>Instrucciones de redacción y ficha de estudio escrita por el agente anfitrión.</td></tr><tr><td><code><video stem>.docx</code></td><td>Ficha de estudio Word editable, que puede generarse con una plantilla.</td></tr></tbody></table>

## Etapas del procesamiento

1. **Descargar** un vídeo de una URL y sus metadatos, subtítulos, comentarios y marcas de patrocinio; los archivos locales omiten esta etapa.
2. **Extraer** fotogramas muestreados y subtítulos utilizables. Whisper solo se ejecuta si no hay subtítulos o se selecciona `--transcribe whisper`.
3. **Corregir** los fragmentos seleccionados de la transcripción si hay un proveedor disponible; esta etapa se puede omitir.
4. **Crear el bundle** con capítulos, voz, fotogramas, segmentos patrocinados y texto OCR.
5. **Preparar la ficha** a partir del bundle y las imágenes seleccionadas.
6. **Escribir `sheet.json`** en un turno aparte con la sesión actual del agente anfitrión.
7. **Generar** el DOCX editable.

## Límites

<table><thead><tr><th>Configuración</th><th>Qué puedes esperar</th></tr></thead><tbody><tr><td>Subtítulos utilizables</td><td>Se prefieren los subtítulos; Whisper no se ejecuta, así que la GPU no afecta a la transcripción.</td></tr><tr><td>Sin subtítulos, GPU NVIDIA y bibliotecas CUDA</td><td>Whisper usa CUDA <code>int8_float16</code>; si falla, se descarta el resultado parcial y la transcripción se reinicia en CPU <code>int8</code>.</td></tr><tr><td>Sin GPU NVIDIA</td><td>Whisper se ejecuta en CPU con <code>int8</code>.</td></tr></tbody></table>

En el mismo vídeo de 43 minutos, v0.6.5 redujo el análisis automático de 2 091 a 518 segundos usando subtítulos. Sin subtítulos, Whisper tardó 273 segundos con una RTX 3050 Ti y CUDA, frente a unos 1 144 segundos en CPU. La carpeta de análisis pasó de 421 MB a 213 MB; las bibliotecas CUDA opcionales ocupan alrededor de 1 GB.

<table><thead><tr><th>Configuración de IA</th><th>Qué funciona</th></tr></thead><tbody><tr><td>Anfitrión Claude Code o Codex</td><td>Puede corregir la transcripción y escribir la ficha con el modelo configurado en su sesión.</td></tr><tr><td>Anfitrión OpenClaw</td><td>Hay que seleccionarlo explícitamente; puede corregir la transcripción y escribir la ficha. La corrección en vivo no está verificada.</td></tr><tr><td>Ruta SDK de OpenAI o local</td><td>Solo corrige la transcripción; estas rutas SDK no escriben la ficha.</td></tr><tr><td>Sin anfitrión ni proveedor SDK</td><td>Tras la configuración, la extracción y <code>bundle.json</code> siguen funcionando sin conexión. Se omite la corrección y no está disponible la redacción automática de la ficha.</td></tr></tbody></table>

## Confidencialidad

La corrección remota de la transcripción envía el texto transcrito y contexto limitado del vídeo, pero no el vídeo original, el audio, los fotogramas, el texto OCR ni la clave de API. La redacción remota de la ficha puede recibir el texto del bundle, metadatos, OCR y fotogramas seleccionados, pero no el vídeo ni el audio originales. Para contenido local, se pide consentimiento antes de procesarlo a distancia. Usa `--no-correct` para mantener la corrección sin conexión. Obtener un vídeo desde una URL es una acción de red independiente.

## Estado

v0.6.5 midió `DEFAULT_CHECKS=145` (145 correctas, 0 fallidas) y `FULL_CHECKS=147` (147 correctas, 0 fallidas). Consulta las [notas de la versión](RELEASE_NOTES.md).

## Licencia

MIT — consulta [LICENSE](LICENSE).
