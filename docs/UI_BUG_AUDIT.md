# AIStoryWriter — UI / Bug Audit

Fecha de revisión: 2026-09-30  
Branch revisado: `bugfixes`  
Commit revisado: `9d6ffc2abdd2f569eaddc6f9ef80b684210cb052`

> Auditoría estática del código del branch `bugfixes`. Los hallazgos describen problemas observables en el flujo del código; no sustituyen una prueba manual de ejecución.

## Prioridad alta

### 1. `ui/chat.py` + `ui/main.py` — Las actualizaciones de proyecto no están ligadas al proyecto que ejecutó la tarea

**Prioridad:** 🔴 Alta

`ChatPanel._on_step_finished()` emite `project_updated` durante los pasos del workflow.  
`MainWindow` conecta esa señal directamente con `_on_ai_task_finished()`, que recarga siempre `self._current_project.id`.

Esto crea un problema cuando:

1. Se inicia una generación para el proyecto A.
2. La tarea sigue ejecutándose.
3. El usuario cambia al proyecto B.
4. Termina un paso de la tarea de A.
5. `MainWindow` puede intentar refrescar B usando eventos producidos por A.

**Riesgo:** refrescos incorrectos, estado visual desincronizado y posible mezcla entre proyecto activo y proyecto en ejecución.

**Corrección propuesta:** asociar cada workflow/thread con un `project_id` inmutable y hacer que `MainWindow` procese la actualización únicamente cuando corresponda al proyecto que ejecutó la tarea. Además, distinguir entre eventos de progreso por paso y el evento final de la tarea.

---

### 2. `ui/story.py` + `ui/main.py` — Se pueden perder cambios no guardados mientras corre una tarea de IA

**Prioridad:** 🔴 Alta

Durante una generación, `MainWindow._on_chat_busy_changed()` marca el panel Story como ocupado, pero los editores de texto pueden continuar siendo editables.

Después, `_on_ai_task_finished()` vuelve a cargar el proyecto desde almacenamiento.

Como `ChatPanel._on_step_finished()` puede provocar actualizaciones durante el workflow, un cambio manual realizado mientras la IA trabaja puede ser reemplazado por el contenido guardado anteriormente.

Afecta especialmente a:

- Synopsis
- Outline
- World
- Chapters
- Author Profile

**Corrección propuesta:** durante una operación que pueda sobrescribir el proyecto, bloquear los editores afectados; o, alternativamente, mantener el estado sucio en UI y diferir el reload hasta que la tarea termine, haciendo merge explícito si corresponde.

---

### 3. `ui/images.py` — Una generación de imagen puede guardarse en el proyecto equivocado al cambiar de proyecto

**Prioridad:** 🔴 Alta

`_on_generate_requested()` crea el thread de imagen sin capturar de forma inmutable el proyecto propietario de la operación.

En `_on_generation_finished()`, el resultado se guarda usando `self._project.id`, es decir, el proyecto que esté seleccionado en el momento de terminar.

Escenario:

1. Se genera una imagen para A.
2. El usuario cambia a B.
3. La generación termina.
4. El resultado puede guardarse bajo B.

**Riesgo:** imagen asociada al proyecto incorrecto.

**Corrección propuesta:** guardar `project_id` al iniciar el worker y usarlo exclusivamente al finalizar. El resultado debe persistirse contra ese ID, independientemente del proyecto actualmente visible.

---

### 4. `ui/story.py` — Los ajustes de generación de retratos de personajes pueden quedar obsoletos

**Prioridad:** 🔴 Alta

`CharactersTab` carga sus settings al inicializarse, pero en la ruta revisada no se observa un mecanismo equivalente a `set_settings()` conectado desde `MainWindow`.

`MainWindow._on_settings_changed()` actualiza Chat, Images y Models, pero no sincroniza explícitamente los settings del panel de Characters.

**Consecuencia:** si el usuario cambia posteriormente:

- Image Model
- Text Encoder
- VAE
- otros parámetros de imagen

el botón de generar retrato puede continuar utilizando valores antiguos.

Esto también puede provocar que un modelo configurado después del arranque siga apareciendo como no disponible para Character Image Generation.

**Corrección propuesta:** implementar una actualización centralizada de settings para `CharactersTab` cuando cambien desde Settings.

---

### 5. `ui/story.py` — La generación de retrato de personaje puede romperse si se cambia de proyecto durante la operación

**Prioridad:** 🔴 Alta

El worker de imagen conserva información del proyecto/personaje original, pero el handler de finalización busca y actualiza el personaje usando `self._project`, que puede haber cambiado mientras la generación estaba corriendo.

Escenario:

1. Se genera retrato para personaje X del proyecto A.
2. El usuario cambia a B.
3. Termina el worker.
4. El callback busca el personaje dentro de B.

Al usar IDs distintos por proyecto, el personaje original probablemente no se encuentra.

Además, la generación puede haber guardado ya el recurso binario dentro de A, pero el `image_ref` del personaje no llega a persistirse correctamente.

**Riesgo:** recurso huérfano y personaje sin referencia a su imagen.

**Corrección propuesta:** capturar `project_id` + `character_id` al iniciar y actualizar el modelo almacenado del proyecto original, no el proyecto visible actual.

---

## Prioridad media

### 6. `ui/settings.py` — `ModelPicker.set_value("")` no limpia el modelo mostrado

**Prioridad:** 🟠 Media

Código actual:

```python
def set_value(self, path: str) -> None:
    for i in range(self.combo.count()):
        if self.combo.itemData(i) == path:
            self.combo.setCurrentIndex(i)
            return
    if path:
        self.combo.addItem(Path(path).name, path)
        self.combo.setCurrentIndex(self.combo.count() - 1)
```

Cuando `path == ""`, no se selecciona explícitamente el estado «— not assigned —».

Si se cambia de un proyecto con modelo asignado a otro sin modelo, el combo puede seguir mostrando visualmente el modelo del proyecto anterior.

**Corrección propuesta:** cuando `path` esté vacío, seleccionar explícitamente el índice de «— not assigned —».

---

### 7. `ui/projects.py` — Renombrar el proyecto no actualiza inmediatamente `MainWindow._current_project`

**Prioridad:** 🟠 Media

`_rename_project()` persiste el nuevo nombre y hace `refresh()`, pero no actualiza el objeto `Project` que conserva `MainWindow`.

**Consecuencias posibles:**

- el título de la ventana puede seguir mostrando el nombre anterior;
- prompts que usan `project.title` pueden continuar usando el valor viejo;
- otros paneles pueden seguir mostrando metadata desactualizada hasta recargar.

**Corrección propuesta:** emitir una señal de proyecto editado/renombrado y actualizar el objeto actualmente cargado en `MainWindow`.

---

### 8. `ui/projects.py` — El clic derecho también puede abrir el proyecto

**Prioridad:** 🟠 Media

`ProjectListRow.mousePressEvent()` emite `open_requested` sin comprobar el botón del mouse.

Eso significa que un clic derecho puede ejecutar la acción de apertura además de mostrar el menú contextual.

**Corrección propuesta:** emitir `open_requested` únicamente para `Qt.LeftButton`.

---

### 9. `ui/projects.py` — Se pierde el resaltado de selección después de un refresh

**Prioridad:** 🟠 Media

`ProjectsPanel.refresh()` reconstruye el listado mediante `clear()` + repoblación, pero no restaura el `project_id` seleccionado.

El proyecto puede seguir abierto en `MainWindow`, mientras que visualmente ninguna fila aparece seleccionada.

Esto es especialmente visible después de editar metadata y disparar `projects_panel.refresh()`.

**Corrección propuesta:** guardar el ID seleccionado antes de reconstruir la lista y restaurar la selección después de `_populate()`.

---

### 10. `ui/chat.py` — El attachment temporal de capítulo puede cruzarse entre proyectos

**Prioridad:** 🟠 Media

`_select_chapter_to_attach()` guarda el capítulo pendiente en `_pending_chapter_attachment`.

Al cambiar de proyecto, `load_project()` no limpia ese attachment.

Escenario:

1. El usuario selecciona un capítulo del proyecto A.
2. Cambia al proyecto B.
3. Envía un mensaje.
4. El mensaje podría llevar contenido del capítulo de A.

**Corrección propuesta:** limpiar el attachment al cambiar de proyecto, o asociarlo a un `project_id` y descartarlo automáticamente cuando no coincida con el proyecto actual.

---

## Prioridad media / mejora de persistencia

### 11. `ui/images.py` — Las imágenes generadas genéricamente no se restauran en la galería al reabrir/cambiar de proyecto

**Prioridad:** 🟡 Media

`_on_generation_finished()` guarda las imágenes en `images/`, pero `ImagesPanel.load_project()` revisado solamente actualiza `_project` y los avisos.

No se observa una reconstrucción de la galería/preview a partir de los recursos persistidos.

**Resultado:** la imagen puede existir correctamente en almacenamiento, pero desaparecer de la vista al cambiar de proyecto o reiniciar la aplicación.

**Nota:** esto puede considerarse una carencia de persistencia de UI más que un bug de almacenamiento.

**Corrección propuesta:** cargar y enumerar los recursos de imagen del proyecto al hacer `load_project()` y reconstruir la galería.

---

### 12. `ui/search.py` — Problema de HTML escaping en `_highlight()`

**Prioridad:** 🟡 Media

La función prepara una versión escapada, pero en el camino de éxito termina aplicando el regex sobre el `text` original:

```python
return pattern.sub(replace, text)
```

El label usa `Qt.RichText`.

Si el texto de una búsqueda contiene caracteres como `<`, `>` o fragmentos con apariencia de HTML, el contenido puede interpretarse como rich text.

**Riesgo:** renderizado incorrecto, pérdida de fragmentos o resultados visuales inesperados.

**Corrección propuesta:** escapar primero el contenido completo y realizar el highlighting sobre la versión segura, conservando correctamente las posiciones o usando una estrategia de tokenización/segments.

---

## Otros bugs / riesgos detectados

### 13. `engine/workflow.py` — Se imprimen prompts completos aunque el setting esté desactivado

**Prioridad:** 🟡 Media

`_run_inference_v2()` realiza un `print()` directo de los prompts completos.

El código también dispone de `_log_prompt_if_enabled()`, cuyo comportamiento sí está condicionado por el setting correspondiente.

Esto hace que la opción de Settings «Show full prompt sent to the model in console/log» no controle todos los lugares donde se imprimen prompts.

**Riesgos:**

- consola llena de texto;
- logs innecesariamente grandes;
- exposición de contenido de proyecto/prompts;
- el ajuste de privacidad/diagnóstico no se respeta completamente.

**Corrección propuesta:** eliminar el `print()` incondicional o protegerlo con el mismo setting.

---

### 14. `ui/images.py` + `engine/image_engine.py` — Riesgo por generación simultánea usando un singleton del motor de imagen

**Prioridad:** 🟡 Media

`get_image_engine()` devuelve una instancia singleton del motor de imágenes.

ImagesPanel y CharactersTab pueden iniciar sus propios workers, pero ambos pueden terminar accediendo al mismo estado del motor.

No se observa una serialización global de:

- carga del modelo;
- cambio de backend/modelo;
- generación;
- unload.

**Riesgo:** operaciones concurrentes sobre estado compartido, especialmente si el modelo/backend cambia mientras otro worker está utilizando el engine.

**Corrección propuesta:** serializar el acceso al singleton mediante lock/queue, o crear instancias aisladas por worker si el backend lo permite y el consumo de VRAM/RAM resulta aceptable.

---

## Hallazgos adicionales relacionados con el flujo de UI

### 15. `agents/manager.py` — La indicación «Next» puede elegir una acción incorrecta para un capítulo existente pero vacío

**Prioridad:** 🟡 Media

La lógica revisada sigue esencialmente esta secuencia:

```python
if not project.synopsis:
    return WRITE_SYNOPSIS

if not project.outline:
    return GENERATE_OUTLINE

for ch in project.chapters:
    if not ch.reviewed:
        return REVIEW_CHAPTER
```

Después calcula el siguiente capítulo con:

```python
next_num = len(project.chapters) + 1
```

Esto genera dos problemas potenciales:

1. Un capítulo existente pero todavía sin contenido puede entrar en la ruta de «Review Chapter» en lugar de «Write Chapter».
2. `len(chapters) + 1` supone numeración continua. Si hay huecos, eliminaciones o reordenamientos, el número calculado puede apuntar a un capítulo existente.

**Corrección propuesta:** distinguir explícitamente entre capítulo sin contenido, capítulo escrito pero no revisado y capítulo revisado; y calcular el siguiente número a partir del máximo número existente, no solamente de `len()`.

---

## Prioridad sugerida para corregir

### P0 — Antes de seguir agregando funcionalidades

- #1 Identidad de proyecto en tareas/background threads.
- #3 Guardado de imágenes en el proyecto correcto.
- #5 Retratos de personajes al cambiar de proyecto.
- #2 Protección de cambios no guardados durante generación.

### P1 — Correcciones de consistencia de UI

- #4 Sincronización de settings de Characters.
- #6 Reset del ModelPicker.
- #7 Renombrado y estado de `MainWindow`.
- #8 Clic derecho.
- #9 Restaurar selección.
- #10 Limpiar attachments al cambiar de proyecto.

### P2 — Persistencia, robustez y mantenimiento

- #11 Restaurar galería de imágenes.
- #12 Escaping del buscador.
- #13 Respetar `log_full_prompts`.
- #14 Serialización del image engine.
- #15 Corregir lógica de «Next».

## Nota sobre verificación

Esta lista corresponde a una auditoría estática del código disponible en `bugfixes`. Los hallazgos deben validarse con pruebas manuales o tests automatizados, especialmente los escenarios de cambio de proyecto durante operaciones largas.

## Archivos principalmente involucrados

- `ui/main.py`
- `ui/chat.py`
- `ui/story.py`
- `ui/images.py`
- `ui/settings.py`
- `ui/projects.py`
- `ui/search.py`
- `engine/workflow.py`
- `engine/image_engine.py`
- `agents/manager.py`
