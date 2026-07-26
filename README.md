# AI Story Studio

Aplicación de escritorio para escribir novelas con modelos GGUF locales (llama.cpp).
No es un chatbot genérico: todo gira en torno a un proyecto de novela — sinopsis,
outline, personajes, capítulos, memoria de la historia y un chat contextual sobre
esa historia específica.

Todo corre 100% local. No usa internet, no usa la nube, no usa APIs externas.
Los datos se guardan como Markdown + JSON en la carpeta `data/`.

---

## 1. Requisitos

- **Windows** (los scripts `.bat` son para Windows; en Mac/Linux se puede correr
  el equivalente a mano, ver sección 7).
- **Python 3.12+** instalado y agregado al PATH.
  Descargalo de https://www.python.org/downloads/ — durante la instalación
  marcá la casilla **"Add python.exe to PATH"**.
- Al menos un **modelo GGUF** descargado en tu disco (por ejemplo desde
  Hugging Face). Cuanto más grande el modelo, más VRAM/RAM necesita.
- GPU NVIDIA (opcional, pero recomendado). Si tenés una GTX serie 16
  (1650/1660/1660 Ti/1660 Super) la app lo detecta solo y ajusta la
  configuración automáticamente — no hay que tocar nada.

---

## 2. Instalación (una sola vez)

1. Descomprimí el `.zip` en la carpeta donde quieras tener el proyecto.
2. Doble click en **`setup.bat`**.

Esto hace, en orden:
- Crea un entorno virtual en `.\venv`.
- Instala **PySide6** (la interfaz gráfica).
- Instala **llama-cpp-python** con soporte CUDA (si tenés CUDA Toolkit +
  Build Tools + CMake instalados). Si la compilación con CUDA falla, cae
  automáticamente a la versión CPU para que la app funcione igual (más lenta).

Si `setup.bat` falla instalando con CUDA, la app sigue siendo usable —
solo que la generación de texto va a ser más lenta (corre en el CPU).

---

## 3. Ejecutar la app

Doble click en **`run.bat`**. Esto:
- Activa el entorno virtual.
- Fija la variable `GGML_CUDA_FORCE_MMQ=1` (necesaria si tenés una GPU sin
  Tensor Cores reales, como la serie GTX 16).
- Abre la ventana de la app.

Se abre además una **consola** con logs en tiempo real (qué modelo se está
cargando, qué tarea está corriendo, errores, etc.) — dejala abierta atrás de
la ventana, sirve para diagnosticar problemas.

---

## 4. Primer uso — configurar los modelos

Antes de poder generar nada, la app necesita saber **dónde están tus `.gguf`**
y **qué modelo usar para cada tarea**.

1. Andá a la pestaña **Settings** (abajo a la derecha, dentro de un proyecto —
   ver paso 5 si todavía no creaste uno).
2. En **"Models Directory"**, apretá "Browse…" y elegí la carpeta donde
   tenés tus archivos `.gguf` (busca recursivamente en subcarpetas).
3. (Opcional) Ajustá:
   - **Context Size**: cuánto contexto soporta tu modelo (4096 por defecto).
   - **GPU Layers**: cuántas capas mandar a la GPU. `0` = solo CPU.
     Para GPUs de 6GB con un modelo de ~12B en Q4, probá empezar con
     valores altos (30-40) e ir bajando si te quedás sin VRAM.
   - **CPU Threads**: núcleos de CPU a usar.
   - **Temperature**: qué tan creativo/aleatorio es el modelo (0 = predecible,
     2 = muy aleatorio). Este valor pisa el que cada tarea trae por defecto —
     te da control total.
   - **Custom System Prompt**: instrucciones extra que se agregan a *todos*
     los prompts (tono, estilo, reglas de contenido, lo que necesites).
4. Apretá **"Save App Settings"**.
5. Andá a la pestaña **Models** (dentro del proyecto) y asigná qué modelo
   `.gguf` usa cada tarea (Chat, Write Chapter, Review Chapter, etc.).
   Podés usar el mismo modelo para todo con "Assign to All", o uno distinto
   por tarea (por ejemplo un modelo grande para escribir capítulos y uno
   chico/rápido para resumir la conversación).

---

## 5. Crear y manejar proyectos ("stories")

En el panel izquierdo:
- **`+`** (arriba) → crea un proyecto nuevo (te pide título y, opcional,
  una sinopsis inicial).
- Click en un proyecto de la lista → lo abre.
- **🗑** (a la derecha de cada proyecto) → lo borra (pide confirmación,
  **no se puede deshacer**).
- Click derecho sobre un proyecto → menú con Abrir / Renombrar / Borrar.
- Buscador arriba de la lista → filtra por título.

---

## 6. El flujo de escritura (pestaña **Story**)

El pipeline pensado es:

```
Synopsis → Outline → Review Outline → Write Chapter → Review Chapter
→ Update Memory → (siguiente capítulo) → repetir
```

Dentro de la pestaña Story vas a encontrar sub-secciones:

- **Synopsis**: botón para generar o editar a mano la sinopsis general.
- **Outline**: genera el outline capítulo por capítulo; también se puede
  pedir una revisión (Review Outline).
- **Characters**: agregá personajes a mano ("+ Add Character") o dejá que
  se vayan mencionando en la historia; cada uno tiene nombre, rol,
  descripción, backstory y traits.
- **Chapters**: lista de capítulos. Desde ahí:
  - **Write Chapter** → genera el próximo capítulo usando la sinopsis,
    outline, memoria y personajes como contexto.
  - **Review Chapter** → el modelo revisa consistencia, prosa, ritmo, etc.
  - **Update Memory** → extrae lo que pasó en el capítulo y lo guarda en
    la "Story Memory" (para que capítulos futuros no pierdan continuidad).
  - **Delete** (capítulo) → borra un capítulo puntual.
  - **+** (arriba de la lista) → agrega un capítulo vacío a mano.
- **World**: notas de worldbuilding (libres, editables a mano).
- **Memory**: la memoria acumulada de la historia — se actualiza sola con
  "Update Memory", pero también la podés editar directamente si el modelo
  se equivocó en algo.

Cualquier tarea que dispares desde Story (generar sinopsis, escribir
capítulo, etc.) te lleva automáticamente a la pestaña **Chat**, donde ves
el texto generándose token por token en vivo.

---

## 7. Chat

Es una conversación **sobre esa historia puntual** — no un chat genérico.
Podés:
- Pedir que reescriba una escena.
- Preguntar detalles de continuidad ("¿de qué color era el auto de X?").
- Pedir brainstorming de ideas para el próximo capítulo.
- Pedir cambios de personajes.

Mientras el modelo responde, el botón "Send" cambia a **"Stop"** — apretalo
si querés cortar la generación a mitad de camino (el texto generado hasta
ese punto queda guardado, no se pierde).

La conversación **nunca se borra**: cuando se acumulan muchos mensajes,
los más viejos se resumen automáticamente (no se le mandan al modelo en
crudo, para no gastar contexto) pero siguen visibles en pantalla para
siempre.

---

## 8. Logs

`run.bat` abre una consola con logs de todo lo que pasa: qué modelo se
carga, qué tarea corre, cuántos tokens generó, errores de inferencia, etc.
Si algo no anda como esperás, mirá esa consola primero — suele decir
exactamente qué pasó.

---

## 9. Dónde se guardan tus datos

Todo queda en `data/<id-del-proyecto>/` como archivos de texto planos que
podés abrir con cualquier editor:

```
data/<project-id>/
  project.json       ← metadata (título, fechas, asignación de modelos)
  synopsis.md
  outline.md
  world.md
  memory.md
  story.md            ← la novela completa concatenada, para leer
  chat.json           ← historial completo del chat
  characters/*.json
  chapters/chapter_XXX.json + chapter_XXX.md
```

Nada de esto usa una base de datos — podés copiar la carpeta entera a otra
máquina, subirla a un pendrive, versionarla con git, lo que quieras.

---

## 10. Problemas comunes

- **"No model assigned for task"** → andá a la pestaña Models y asigná un
  `.gguf` a esa tarea (o usá "Assign to All").
- **"llama-cpp-python is not installed"** → corré `setup.bat` de nuevo, o
  instalalo a mano dentro del venv: `pip install llama-cpp-python`.
- **Se queda sin VRAM / crashea al cargar el modelo** → bajá "GPU Layers"
  en Settings, o usá un modelo más chico / más cuantizado (Q4 en vez de Q8).
- **La generación es muy lenta** → si tenés GPU y `GPU Layers` está en 0,
  subilo. Confirmá en la consola de logs que dice `force_mmq`/`flash_attn`
  correctamente para tu tarjeta.
