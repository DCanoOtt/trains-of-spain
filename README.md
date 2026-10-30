# Monitor de trenes RENFE

Mapa en tiempo real de los trenes de Renfe (Cercanías/Rodalies y Alta Velocidad, Larga y Media Distancia) sobre el trazado de vías de la península ibérica. Cada tren se dibuja como una flecha que indica el sentido de la marcha, con su tipo, su retraso o adelanto y una etiqueta con su código.

## Instalar y ejecutar

```bash
git clone https://github.com/DCanoOtt/trains-of-spain.git
cd trains-of-spain
python3 server.py        # puerto 8765 por defecto; opcional: python3 server.py 9000
```

Abre http://127.0.0.1:8765. Solo hace falta Python 3.8 o superior (sin dependencias). Hay que abrir la página a través del servidor: abrir `index.html` con doble clic no da tipos de tren ni retrasos.

**Primera ejecución.** El servidor descarga y guarda en `.cache/`:
1. el GTFS estático de Renfe, para conocer el tipo, el número y el destino de cada tren;
2. el trazado de las vías desde OpenStreetMap (Overpass), en 15 zonas.

Puede tardar entre 1 y 10 minutos; mientras tanto la página funciona y avisa del estado. Después arranca al instante. Para volver a descargarlo todo, borra la carpeta `.cache/`. Estado en http://127.0.0.1:8765/api/status

## Funciones

**Mapa**
- Fondo de OpenStreetMap en tres versiones: **gris** (por defecto), oscuro y en color. No necesitan clave.
- Vías, a elegir:
  - **OpenRailwayMap** (por defecto): capa de imágenes con todas las vías.
  - **Vectoriales (servidor):** red completa dibujada en vectores a partir de la caché de `server.py`; alta velocidad en rojo.
  - **Vectoriales por zona:** consulta directa a Overpass de lo que se ve en pantalla (hay que acercar el zoom).
  - Ocultas.
- Si las vías vectoriales no cargan, una caja sobre el mapa explica el motivo.

**Trenes**
- Flecha orientada según el rumbo del feed o, si no lo trae, según el movimiento del tren. Hasta que un tren se mueve una vez aparece como círculo. Los rumbos se guardan 10 minutos en el navegador.
- Selector de tipo: Cercanías, AVE, AVE Int., AVLO, Avant, Avant Exp., Alvia, Euromed, Intercity, Trencelta, Media Distancia, Regional, Regional Exp. y Proximidad, con contador y color por tipo.
- Etiqueta junto al tren con su código y el retraso (+) o adelanto (−) en minutos. Modo «auto»: se muestran con zoom alto o con pocos trenes a la vista.
- Color de los puntos por tipo o por retraso; filtro por retraso (≥ 5′, ≥ 15′, ≥ 30′, adelantados, sin dato); lista ordenable por línea, más retraso o más adelanto.
- Resumen: trenes totales, retraso medio y trenes con ≥ 5′.
- Búsqueda por número de tren, línea, destino o parada.
- Actualización automática cada 15 s, 30 s, 1 min o 2 min. Renfe publica datos nuevos aproximadamente cada 30 s, así que bajar de ahí rara vez aporta algo; el servidor guarda cada feed 15 s.
- Botón **Demo**: trenes simulados para probar la interfaz sin conexión.

## Fuentes de datos (abiertas, sin clave)

| Dato | Fuente |
|---|---|
| Posiciones | `gtfsrt.renfe.com/vehicle_positions.json` (Cercanías) y `vehicle_positions_LD.json` (AV/LD/MD) |
| Retrasos y supresiones | `gtfsrt.renfe.com/trip_updates.json` y `trip_updates_LD.json` |
| Tipo, número y destino | GTFS estático `ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip` |
| Vías (vectoriales) | OpenStreetMap vía Overpass, simplificadas y cacheadas por el servidor |
| Vías (imágenes) | OpenRailwayMap |
| Fondo | OpenStreetMap (gris y oscuro con filtros CSS) |

## Notas y límites

- Los trenes se pintan en las coordenadas que publica Renfe; no se ajustan geométricamente a la vía.
- **Retrasos.** Renfe puede no publicar retraso para todos los trenes (por ejemplo, solo los que se desvían del horario). Los que no tienen dato aparecen como «s/d», no como puntuales. El panel indica cuántos trenes tienen tipo, retraso y sentido conocidos.
- **Tipo.** En Larga/Media distancia sale del GTFS estático, cruzando por el identificador del viaje o por el número de tren. Los que no se pueden cruzar aparecen como «Otros». Cercanías no se subdivide por tipo.
- **Suprimidos.** Un viaje se marca como «suprimido» solo si el aviso de Renfe coincide con su identificador exacto y con su fecha de servicio. Aun así, Renfe puede publicar la posición de un tren cuyo viaje consta como cancelado (por ejemplo, material que circula en vacío); la ficha del tren muestra el aviso original en la fila «Aviso Renfe».
- Si abres `index.html` sin `server.py`, se intenta el feed directo (sin tipos ni retrasos y sujeto a CORS).
- Datos y marca: Renfe. Mapa y vías © OpenStreetMap. Proyecto no oficial.
