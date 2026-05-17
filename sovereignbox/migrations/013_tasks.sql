-- Migración 013: extiende la tabla tasks existente con `prioridad` y agrega
-- los índices de pendientes y due_date.
--
-- La tabla `tasks` ya existe del schema original con: id, title, description,
-- assigned_to, due_datetime, task_status (enum: pendiente|en_progreso|completada|
-- cancelada), recurrence, reminder_sent_at, llm_raw_output, created_by,
-- created_at, updated_at, deleted_at.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS prioridad TEXT NOT NULL DEFAULT 'normal'
        CHECK (prioridad IN ('baja', 'normal', 'alta'));

CREATE INDEX IF NOT EXISTS idx_tasks_pendientes
    ON tasks (assigned_to, prioridad, due_datetime)
    WHERE task_status = 'pendiente' AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_due_pendientes
    ON tasks (due_datetime)
    WHERE task_status = 'pendiente' AND deleted_at IS NULL AND due_datetime IS NOT NULL;
