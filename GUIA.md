# 📘 Guía de uso — SovereignBox

Asistente familiar privado: anota gastos, organiza la agenda, lleva la lista de compras, recuerda los vencimientos y guarda las boletas. **Todo se hace de dos formas:** por Telegram (rápido, desde el cel) o por la web (más visual, mejor para ver y editar).

> 💡 Lo más cómodo es **mandarle todo al bot por Telegram** y usar la web solo para revisar al final del día o de la semana.

---

## 🤖 Por Telegram — la forma más rápida

### 1. Anotar gastos e ingresos

Escribí **como hablás**. El bot entiende lenguaje natural.

**Gastos (lo más común):**
```
gasté 25€ en farmacia
pagué 80 en el super
puse 50 de nafta
compré 200€ en el super con la tarjeta
30 en la peluquería
gasto de 15€ en netflix
```

**Ingresos:**
```
cobré el sueldo 1500€
cobró Luisiana con Constan 800
llegó el assegno 200€
```

**Pagos con la tarjeta de crédito:**
Si decís "con la tarjeta" o "con visa" lo carga en la **Visa Hector** (no en la cuenta corriente). Sin esa palabra clave, lo carga en la cuenta del que está mandando el mensaje.

```
compré 200€ en zara con la tarjeta
350 con visa en farmacia
```

**Pagos en efectivo:**
```
50 en efectivo del fondo de casa
gasté 30 cash en verdulería
```

**Audio:**
También podés mandar un audio de voz: "gasté veinticinco euros en farmacia". El bot lo transcribe y procesa igual.

**Si hay duda:** el bot te muestra los datos extraídos antes de guardar y vos confirmás con un botón ✅ o ❌.

**Si te equivocaste:**
```
/undo
```
borra el último movimiento que cargaste.

---

### 2. 📸 Subir boletas y comprobantes

Sacale **foto** a la boleta (gas, luz, agua, lo que sea) y mandala al bot. Podés agregarle un texto al lado:

```
[foto] boleta del gas €80 vence el 25
```

El bot:
1. Guarda la foto.
2. Crea un movimiento "pendiente" con esos datos.
3. Te avisa que está pendiente de pago.

**Cuando lo pagues**, andá a la web → tab **Pendientes** → click en `✓ Pagar` y subís el comprobante.

**¿Dónde guardo el papel?** En la web, cualquier archivo tiene un botón 📁 que te dice dónde archivarlo físicamente: ej. *"Servicios → Gas → 2026 - Mayo"*.

---

### 3. 🛒 Lista de compras

La lista es **familiar**: Hector y Luisiana ven y editan la misma.

**Agregar:**
```
anotame leche
necesito 2kg de tomate
comprar yogur, pan y queso
```
El bot entiende cantidades y unidades automáticamente.

**Ver lo que falta:**
```
/compras
```

**Volver del super (vaciar):**
```
/compras done
```
Marca todo como comprado de un saque.

**Borrar todo sin marcar:**
```
/compras clear
```

---

### 4. 📅 Agenda — turnos médicos, eventos del colegio, trámites

Decile al bot lo que tenés que recordar **con día y hora**:

```
el jueves a las 10 dentista de Sofía
mañana 15hs reunión escuela de Noah
el 20 trámite en el banco
viernes 18hs cumple de mamá
```

El bot:
- Detecta el día y la hora.
- Categoriza automáticamente (médico, colegio, burocracia, familia).
- **Te manda un recordatorio por Telegram** antes (24h antes para médicos/colegio/burocracia, 2h antes para el resto — ajustable desde la web).

**Ver la agenda:**
```
/agenda           — próximos 7 días
/agenda hoy       — solo hoy
/agenda manana    — solo mañana
/agenda mes       — próximos 30 días
```

---

### 5. ✅ Tareas (TODOs)

Cosas que tenés que hacer pero **sin fecha exacta**: reparar algo, llamar a alguien, devolver libros.

**Agregar:**
```
tengo que reparar la canilla
hay que llamar al gas
recordame sacar la basura
```

**Asignar a alguien específico:**
```
@lu llevar libros a la biblioteca
@hector pagar el patente del auto
```

**Marcar urgente:**
```
urgente llamar al pediatra
```
Queda como prioridad alta (🔴) y aparece arriba de la lista.

**Ver tareas pendientes:**
```
/tareas         — las mías + sin asignar
/tareas todas   — todas las de la familia
/tareas hechas  — completadas recientes
```

**Marcar una como hecha:**
```
/tarea done canilla
```
Busca por substring del título.

---

### 6. 📊 Ver el estado financiero por Telegram

```
/resumen      — balance del mes (ingresos, gastos, ahorro)
/gastos       — detalle de gastos variables del mes
/ingresos     — detalle de ingresos por persona
/presupuesto  — límites mensuales y % usado
/proyeccion   — cuánto vas a gastar a fin de mes al ritmo actual
```

**Setear un presupuesto:**
```
/presupuesto Supermercado 400
```

---

### 7. 🔔 Avisos automáticos

El bot te manda **solo** sin que abras nada:

| Cuándo | Qué te avisa |
|---|---|
| **Cada día 08:00** | Eventos de hoy si tenés alguno |
| **Lunes 08:00** | Resumen semanal: agenda + tareas pendientes + balance de la semana pasada |
| **Día 1 del mes 09:00** | Resumen del mes anterior (top 3 gastos, tasa de ahorro) |
| **Cuando excedés un presupuesto** | "⚠️ Presupuesto excedido — Supermercado · Gastaste €450 de €400 (113%)" |
| **Antes de un vencimiento** | Cuota de préstamo o resumen de tarjeta a 1-2 días |
| **Gasto inusual** | "🔍 Gasto inusual detectado — Salud: €170 (+100% vs habitual)" |
| **24h antes de un evento médico** | "🏥 Dentista Sofía · 15/05 · 10:00" |

**Activar/desactivar avisos:**
```
/notif                — ver tus preferencias
/notif off resumen    — desactivar resumen mensual
/notif on budget      — activar alertas de presupuesto
```

Tipos: `budget` (presupuestos), `reminder` (vencimientos y eventos), `resumen` (semanal y mensual), `anomaly` (gastos inusuales).

---

### 8. 🆘 Ayuda

```
/ayuda
```
Lista todos los comandos.

---

## 🌐 Por la web

Abrí http://localhost:8080 (o la URL que tengas configurada).

Hay **12 tabs** arriba. Te las paso de izquierda a derecha:

### **🌅 Hoy** (la primera, default)

Lo que tenés que saber del día:
- **Saldo del día** arriba a la derecha (cuánto ingresaste y gastaste hoy).
- **Agenda hoy** a la izquierda — clic para ir a la agenda completa.
- **Tareas pendientes** a la derecha — círculo gris para marcarla hecha.
- **Compras pendientes** (top 5) si la lista no está vacía.
- **Boletas por pagar** si hay alguna con vto cercano.

Es la pantalla que abrís a la mañana mientras desayunás.

### **🏛 Bilancio**

Estado financiero del mes seleccionado:
- **KPIs**: ingresos / gastos / balance del mes.
- **Activos / Pasivos / Patrimonio neto**.
- **Tasa de ahorro %** del mes y del año, **autonomía** en meses.
- **Per Membro**: cuánto aporta y gasta cada uno (Hector vs Luisiana).
- **Notas del Contable**: insights automáticos (mayor gasto, comparativa con promedio, anomalías).
- **Objetivos**: presupuestos por categoría con barra de progreso.
- **Gastos fijos** del mes (alquiler, servicios, etc.).
- **Gastos variables** en pie chart.
- **Últimos movimientos**.

Cambiar de mes con las flechas `‹ ›` arriba.

### **🏦 Cuentas**

Saldo actual de cada cuenta:
- Cuenta Hector
- Cuenta Luisiana
- Efectivo casa (ahorros físicos)
- Visa Hector (deuda actual)

**Click en una cuenta** → editar:
- Saldo inicial (si querés corregir el snapshot)
- Fecha del snapshot
- Día de cierre y vencimiento (solo tarjeta)
- Botón **Recalcular** si tenés transacciones retroactivas que no están sumando.

### **✅ Tareas**

CRUD completo de TODOs:
- **Input rápido** arriba: `@lu llevar libros` o `urgente llamar al gas` parsea sin LLM.
- **Filtros**: por asignado (Hector / Luisiana / sin asignar / todos) y por estado.
- **Agrupado por prioridad**: 🔴 alta · ⚪ normal · 🔵 baja.
- Click en el círculo para marcar hecha.
- Dropdown para cambiar prioridad sin abrir nada.

### **📅 Agenda**

Eventos próximos:
- Selector de rango (7 / 14 / 30 / 90 días).
- Lista agrupada por día con headers tipo *"Hoy · 12 de mayo"* / *"Mañana"*.
- Iconos por categoría: 🏥 médico · 🏫 colegio · 📋 burocracia · 👨‍👩‍👧 familia · 📅 otro.
- Click en un evento → editar (cambiar fecha, hora, ubicación, recordatorio).
- Botón **+ Nuevo** para agregar manualmente.

Cada vez que creás o editás un evento, **se reprograma el recordatorio automático** para todos los miembros con Telegram.

### **🛒 Compras**

Lista familiar minimalista:
- **Input** arriba con autoparseo de comas y "y" (`leche, pan, 2kg tomate`).
- Pendientes con círculo to-do.
- Botón **✓ Volví del super** (vaciar todo).
- Histórico con opción **↩ volver a la lista** si lo marcaste por error.

### **💸 Pendientes**

Boletas de servicios (gas, luz, etc.) que cargaste pero todavía no pagaste:
- **+ Subir archivo**: arrastrar un PDF o foto que queda como "huérfano" hasta que lo asocies a un movimiento.
- **Lista de pendientes** con botón `✓ Pagar`.
- **Archivos sin asociar**: lo que mandaste por Telegram sin caption claro.
  - 👁 ver el archivo
  - 📁 preguntarle al asistente dónde guardar el papel físico
  - ✕ borrar

### **💳 Tarjeta**

Todo lo de la Visa:
- **Ciclo en curso**: cuánto vas gastando este ciclo, cuándo cierra, próximo vto.
- **Movimientos del ciclo**: las compras del ciclo abierto.
- **Compras en cuotas**: planes activos con barra de progreso (ej: lavarropas €600/6 cuotas — vas en 2/6).
- **+ Nueva**: cargar una compra en cuotas — el sistema crea automáticamente las N transacciones futuras.
- **Suscripciones**: Netflix, Spotify, etc. — cobros que se repiten cada mes en el día que digas.
- **Resúmenes pagados**: histórico de cierres anteriores.

**Importante**: el día del vencimiento, **el sistema descuenta automáticamente** el monto del resumen de tu cuenta corriente y cancela la deuda en la tarjeta.

### **🏧 Préstamos**

Los préstamos automáticos (€249 hasta 03/2027 y €250 hasta 12/2028):
- Cuántas cuotas restantes y saldo pendiente.
- Cuota mensual total y fecha de cancelación de cada uno.
- Click para editar (si cambió la cuota, agregar última cuota distinta, etc.).
- **+ Nuevo** para cargar un préstamo nuevo.

Cada día 15 a las 06:00 el sistema **inserta automáticamente** la cuota como gasto en la cuenta de débito.

### **📋 Movimientos**

Tabla completa de todas las transacciones:
- Filtros: búsqueda libre, tipo, categoría, subcategoría.
- Botón **↓ CSV** para exportar todo el mes.
- Por fila: ✎ editar · 📎 ver/subir attachments · ✕ eliminar.

### **📊 Presupuestos**

Setear límites mensuales por categoría (ej: Supermercado €400/mes):
- Lista con barra de progreso y semáforo (verde / amarillo / rojo).
- Form abajo para agregar/editar.

### **📈 Análisis**

- **Comparativa anual**: ingresos vs gastos mes a mes, año actual vs anterior (barras).
- **Tendencia mensual**: top 5 categorías o subcategoría seleccionada (líneas, 12 meses).
- **Tasa de ahorro**: % ahorrado mes a mes (barras color-coded).
- **Gasto por categoría**: ranking del mes — **click en cualquier categoría** abre un modal con: total 12m, ticket promedio, mini-gráfico de tendencia y últimas 30 transacciones.

---

## 🎯 Casos de uso comunes (recetas)

### Receta 1: "Acabo de gastar plata"
**Por Telegram** → escribís: `gasté 30 en farmacia`
✅ Listo. Se cargó al saldo de tu cuenta.

### Receta 2: "Llegó la boleta del gas"
**Por Telegram** → sacale foto + caption: `boleta gas 80€ vence 25`
✅ Queda como pendiente. El día 23 te avisa que vence pronto.

### Receta 3: "Pagué la boleta"
**Por web** → tab **Pendientes** → click `✓ Pagar` → te pide el comprobante (subílo).
✅ La deuda baja, la cuenta corriente baja, queda el comprobante archivado.

### Receta 4: "Voy al super y no me olvido nada"
**Antes**: cualquiera dicta `comprar leche, pan, yogur`.
**En el super**: abrís `/compras` o la tab Compras y vas marcando.
**Al volver**: `/compras done` o botón "Volví del super".

### Receta 5: "Tengo turno con el dentista"
**Por Telegram** → `el jueves 10hs dentista de Sofía`
✅ Queda agendado. **24h antes** te llega el aviso.

### Receta 6: "Algo se rompió y hay que arreglar"
**Por Telegram** → `tengo que reparar la canilla del baño`
✅ Queda en tu lista de tareas. La marcás cuando esté hecho.

### Receta 7: "Compré algo grande con la tarjeta a 6 cuotas"
**Por web** → tab **Tarjeta** → **+ Nueva**:
- Descripción: "Lavarropas"
- Monto: 600
- Cuotas: 6
- Fecha de compra: hoy
✅ El sistema genera las 6 cuotas automáticamente. Cada mes aparece una en el resumen.

### Receta 8: "Me suscribí a Netflix"
**Por web** → tab **Tarjeta** → en sección "Suscripciones" → **+ Nueva**:
- Nombre: Netflix
- Monto: 15
- Día del mes: 5
✅ Cada día 5 el sistema lo carga automáticamente como gasto en la tarjeta.

### Receta 9: "Quiero ver cómo va el mes"
**Por Telegram** → `/resumen`. **Por web** → tab **Bilancio**.

### Receta 10: "¿Cuánto debemos en total?"
**Por web** → tab **Bilancio** → KPI **Pasivos** (deuda actual de tarjetas) + tab **Préstamos** (saldo pendiente de préstamos).

---

## 💡 Tips y trucos

- **Audio > teclado**: cuando estás en la calle, mandar audio es más rápido. El bot lo transcribe igual.
- **Sin monto explícito**: si escribís "pan y leche" el bot asume que es lista de compras (sin €). Si escribís "30€ en pan" asume gasto.
- **Errores leves de transcripción**: el bot perdona "daca" → Dacia, "witre" → Windtre, "knap" → Knapp.
- **Si no entendió bien**: te muestra qué entendió antes de guardar. Cancelá con el botón ❌.
- **`/undo` siempre disponible** para deshacer el último movimiento.
- **La web es solo para mirar** la mayoría del tiempo. Casi todo se carga por Telegram.
- **Backup automático**: la base de datos se respalda cada noche a las 03:00. No hay riesgo de perder datos.
- **Privacidad**: 100% local. Nada sale de tu casa. Ni Google ni OpenAI ven nada. El LLM (qwen2.5) corre en el mismo servidor.

---

## 🤔 Preguntas frecuentes

**¿Y si nos vamos de vacaciones y no me acuerdo nada?**
Cuando volvés, abrís el dashboard (tab **Hoy**) y ves todo: tasa de ahorro del mes, gastos pendientes, próximos eventos. Si pasaron varios días, el resumen del lunes te pone al día.

**¿Y si cargo algo mal?**
- Por Telegram: `/undo` (último movimiento) o ir a la web y editarlo.
- Por web: cualquier movimiento se edita o elimina desde la tab Movimientos.

**¿Las cuotas pasadas que no estén cargadas en el sistema?**
No se cargan retroactivamente. Si cargaste el préstamo hoy con `fecha_inicio = mañana`, las cuotas anteriores **no aparecen** porque se asume que ya estaban descontadas de tu saldo actual. Si querés un histórico completo, decile a Hector que use el botón **Recalcular saldo** en la cuenta.

**¿Cómo le aviso a Hector que necesito algo?**
- Si es algo material: lo agregás a `/compras`.
- Si es una tarea: `@hector llamar al colegio` la asigna a él. Cuando él abra `/tareas` la ve.

**¿Y si quiero un resumen rápido en Telegram?**
- `/resumen` — lo financiero del mes
- `/agenda hoy` — eventos
- `/tareas` — TODOs pendientes
- `/compras` — lista del super

**¿Puedo desactivar los avisos en algún momento (vacaciones)?**
Sí: `/notif off` desactiva todos. `/notif on` los reactiva.

---

## ⏰ Recordatorio de los horarios automáticos

El sistema corre estos jobs en horario Europe/Rome:

| Hora | Qué hace |
|---|---|
| 03:00 (UTC) | Backup de la base de datos |
| 06:00 | Genera cuotas de préstamos vencidas hoy |
| 06:05 | Genera cargos de suscripciones vencidos hoy |
| 06:10 | Cierra resúmenes de tarjeta cuyo vto sea hoy |
| 08:00 | Resumen de la agenda del día (si hay eventos) |
| 08:00 (lunes) | Resumen semanal completo |
| 09:00 | Recordatorios de vencimientos próximos (≤2 días) |
| 09:00 (día 1) | Resumen del mes anterior |
| Cada 5 min | Despacha notificaciones pendientes |

No tenés que tocar nada — todo es automático.

---

¿Dudas? Pregúntenle a Hector. 👍
