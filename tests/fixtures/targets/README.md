# Fuentes del descubrimiento de rutas

`sources.json` conserva URL, fecha, HTTP, SHA256 y, para las páginas provinciales/comarcales, el `href` real y su página de procedencia. Capturas limitadas mediante HTTP normal, sin navegador, cookies, proxy configurado por el proyecto ni evasión de bloqueos. Idealista devolvió 403; Pisos.com interrumpió la conexión. Son carencias reales.

Las tablas de provincias del INE y capitales del BOE se cruzan con `data/municipalities.csv` (INE, 01-01-2026). La tabla de capitales del BOE corresponde al padrón de 01-01-2025; sus 50 nombres tienen correspondencia única en el registro INE de 2026. Ceuta y Melilla son ciudades autónomas y no se añaden a las 50 capitales provinciales.

El matching municipal admite denominaciones oficiales, variantes bilingües oficiales, acentos y artículos escritos tras coma. No añade equivalencias históricas no verificadas: por ejemplo, `palma-de-mallorca` no se vincula automáticamente a `Palma`. Fotocasa no incluye provincia en sus rutas municipales; los nombres duplicados se excluyen incluso cuando la página origen es provincial. Habitaclia permite resolverlos con la provincia explícita de la ruta. Las comarcas del bloque público `internal-links-content-child_areas` se conservan como frontera, sin código municipal.

`observed-targets.json` y `replay.checkpoint.json` se generan exclusivamente reproduciendo las capturas incluidas, con siete páginas conocidas y sin peticiones de red. El registro es provisional: cada ruta necesita validar operación y geografía de búsqueda antes de incorporarse a `config/targets.json`. El checkpoint de replay sirve como evidencia; una ejecución HTTP en Actions debe usar su propio checkpoint, para no confundir páginas reproducidas con páginas ya visitadas por ese entorno.

El script productivo es `scripts/discover_targets.py`. Sus límites `--max-pages`, `--max-depth`, `--max-page-bytes` y `--timeout` acotan cada invocación; conserva la frontera en JSON y no modifica la configuración de producción. Para recuperar tras una interrupción abrupta, comprobar que no quede proceso escritor y retirar únicamente su fichero `.lock`; no hay recuperación automática de locks ajenos.
