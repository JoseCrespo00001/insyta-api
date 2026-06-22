---
name: langflow-knowledge
version: 2
updated: 2026-06-22
langflow_version: "1.10.0"
sources:
  - https://docs.langflow.org
  - https://github.com/langflow-ai/langflow
note: >
  Conocimiento curado de Langflow para auditar flujos. Editable: el auditor lo
  recarga en caliente (no hace falta reiniciar). Para refrescarlo desde la doc
  oficial: `uv run python scripts/update_langflow_knowledge.py`.
---

# Langflow — referencia para auditar flujos

Langflow es una herramienta visual (low-code) para construir agentes y pipelines
de IA sobre LangChain/LangGraph. Un flujo es un grafo de **nodos** (componentes)
unidos por **edges** (conexiones puerto→puerto). Se exporta como JSON con
`data.nodes` y `data.edges`; cada nodo tiene `data.type`, `data.node.display_name`
y `data.node.template` (los campos configurables, incl. prompts y modelos).

## Componentes por categoría

### Entrada / Salida
- **Chat Input**: punto de entrada del mensaje del usuario. Casi todo flujo
  conversacional empieza acá.
- **Chat Output**: respuesta final al usuario. Todo flujo debe terminar en una
  salida; si no, no devuelve nada.
- **Text Input / Text Output**: I/O de texto plano (no chat).

### Modelos (LLM)
- **Language Model / OpenAI / Anthropic / Google / Groq / Ollama**: el modelo que
  genera texto. Parámetros típicos: `model_name`, `temperature`, `system_message`.
- Buenas prácticas: temperatura baja para tareas determinísticas (clasificar,
  extraer); modelo más capaz para razonamiento, más barato para tareas simples.

### Agentes
- **Agent**: el componente central moderno. Tiene instrucciones (system prompt),
  un modelo, y un set de **Tools** conectadas. Hace *tool-calling* nativo: decide
  qué herramienta llamar y con qué argumentos, en loop, hasta resolver.
- Un agente sin tools solo "habla": no puede ejecutar acciones reales (pagar,
  trackear, reservar). Si las instrucciones prometen acciones, deben existir las
  tools que las cumplan.
- **Tool mode**: muchos componentes pueden exponerse como *tool* de un agente
  (toggle "Tool mode").

### Tools (herramientas)
- Built-in: **Calculator**, **URL/Web Search**, **API Request**, **Python REPL**,
  **Wikipedia/Search**, etc.
- **Custom Component / Custom Tool**: código propio expuesto como tool.
- **MCP Connection (MCP Tools)**: conecta herramientas externas vía Model Context
  Protocol. Langflow puede ser **cliente MCP** (consumir tools de un server MCP) y
  **servidor MCP** (exponer sus flujos como tools a otros clientes, ej. Cursor).
- **Flow as Tool / Run Flow**: un flujo entero se expone como tool de un agente
  (composición de agentes). Útil para armar jerarquías supervisor→sub-agentes.

### Ruteo y control de flujo
- **If-Else / Conditional Router**: evalúa una condición (`match_text`,
  `operator`) y manda por una rama u otra. Clave cuando hay múltiples intenciones.
- **Loop**: itera sobre una lista.
- **Pass / Listen / Notify**: utilidades de control y paso de datos.
- Patrón **supervisor/router**: un nodo clasifica la intención y deriva al agente
  especializado correcto. Evita que todo caiga en un único agente genérico.

### Prompts y salida estructurada
- **Prompt**: template con variables (`{tema}`, `{contexto}`). Mantener cada
  prompt enfocado en UNA responsabilidad.
- **Structured Output**: fuerza al modelo a devolver JSON con un esquema. Usar
  cuando otro nodo consume el resultado (ej. el router necesita una categoría).

### Memoria
- **Chat Memory / Message History**: mantiene el contexto multi-turno. Sin memoria
  el agente "olvida" lo dicho antes en la conversación.

### Datos / RAG
- **File / Directory / URL**: cargan documentos.
- **Split Text**: chunking.
- **Embeddings + Vector Store** (Astra DB, Chroma, pgvector, etc.): indexan y
  recuperan contexto.
- **Retriever**: trae los chunks relevantes para grounding (RAG). Reduce
  alucinaciones cuando el agente debe responder sobre conocimiento propio.

## Buenas prácticas y anti-patrones (qué evaluar al auditar)

1. **Prompt monolítico**: un solo agente con un prompt enorme que mezcla muchas
   tareas → dividir en agentes especializados coordinados por un router. Más
   preciso, más fácil de mantener, menos respuestas fuera de scope.
2. **Acciones sin tool**: el agente promete hacer algo (cobrar, agendar, devolver)
   pero no tiene la tool/API que lo ejecuta → agregar la herramienta o la API
   Request correspondiente. Si no, "miente" o inventa.
3. **Sin ruteo con múltiples intenciones**: varios agentes pero ningún
   router/condición que decida a cuál mandar → el mensaje cae en el equivocado.
4. **Casos borde sin cubrir**: pedir datos faltantes, ambigüedad, fuera de scope,
   errores de tool, idioma, y **escalamiento a humano**. Un flujo de producción
   los maneja explícitamente.
5. **Sin memoria en multi-turno**: si la charla requiere recordar contexto, falta
   Chat Memory.
6. **Nodos sueltos**: nodos sin edges no se ejecutan → conectarlos o eliminarlos.
7. **Falta entrada o salida clara**: sin Chat Input/Output el flujo no recibe o no
   responde.
8. **Salida no estructurada cuando se consume**: si un paso posterior parsea el
   texto, conviene Structured Output.
9. **Grounding ausente**: si responde sobre conocimiento propio (precios,
   políticas, catálogo) sin RAG/tool, alucina. Sumar retriever o tool de datos.
10. **Temperatura inadecuada**: alta para clasificar/extraer → inconsistencia.

## Checklist de completitud (para puntuar 0-100)

- [ ] Entrada (Chat Input) y salida (Chat Output) presentes y conectadas.
- [ ] Ruteo/condición si hay >1 intención.
- [ ] Cada agente con las tools necesarias para CUMPLIR lo que promete.
- [ ] Manejo de datos faltantes / ambigüedad / fuera de scope.
- [ ] Fallback y escalamiento a humano.
- [ ] Memoria si es multi-turno.
- [ ] Grounding (RAG/tool) si responde sobre conocimiento propio.
- [ ] Sin nodos sueltos; grafo coherente de principio a fin.
- [ ] Structured output donde otro paso consume el resultado.

## Novedades recientes de Langflow (mantener al día)

> Última versión conocida: **Langflow 1.10.0**. MCP y el Agent component son los
> ejes de los releases recientes (1.9.x–1.10.x).

- **Agent component** con tool-calling nativo (reemplaza patrones viejos de
  agentes manuales).
- **MCP**: Langflow como cliente y como servidor MCP.
- **Flows as Tools**: componer agentes usando flujos como herramientas.
- **Structured Output** integrado.
- **Voice mode** y mejoras de despliegue (Langflow como API/servidor).

> Mantené esta sección con los cambios de cada release. Fuente: changelog y docs
> oficiales (ver frontmatter).
