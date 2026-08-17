import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { AckResult } from "../hooks/useBeaverSocket";
import type { ConfigState, ConfigUpdatedResult, PluginInfo } from "../types";

interface Props {
  connected: boolean;
  config: ConfigState | null;
  configUpdateResult: ConfigUpdatedResult | null;
  getConfig: () => void;
  updateConfig: (changes: Record<string, unknown>) => void;
  personas: string[];
  personaAddResult: AckResult | null;
  listPersonas: () => void;
  addPersona: (name: string, content?: string) => void;
  plugins: PluginInfo[];
  pluginToggleResult: AckResult | null;
  listPlugins: () => void;
  togglePlugin: (name: string, enabled: boolean) => void;
  toolManuals: string[];
  manualAddResult: AckResult | null;
  listToolManuals: () => void;
  addToolManual: (name: string, content: string) => void;
}

// [SETTINGS-1] Field metadata — mirrors LIVE_FIELDS/RESTART_FIELDS in web/server.py exactly.
const LIVE_FIELD_META = [
  { key: "model", label: "model", type: "text" as const },
  { key: "temperature", label: "temperature", type: "number" as const, step: "0.1" },
  { key: "max_loops", label: "max loops", type: "number" as const },
  { key: "max_output_chars", label: "max output chars", type: "number" as const },
  { key: "scope", label: "scope (comma-sep)", type: "text" as const },
];
const RESTART_FIELD_META = [
  { key: "num_ctx", label: "num_ctx", type: "number" as const },
  { key: "num_gpu", label: "num_gpu", type: "number" as const },
  { key: "num_thread", label: "num_thread", type: "number" as const },
  { key: "num_batch", label: "num_batch", type: "number" as const },
  { key: "keep_alive", label: "keep_alive", type: "text" as const },
  { key: "low_vram", label: "low_vram", type: "checkbox" as const },
  { key: "base_url", label: "base_url", type: "text" as const },
  { key: "chroma_path", label: "chroma_path", type: "text" as const },
  { key: "embedding_model", label: "embedding_model", type: "text" as const },
];

type FieldValue = string | boolean;

// [SETTINGS-2] Client-side validation, mirrored from what server.py actually
// does with these values (int()/float() casts) so bad input gets a clear
// inline message instead of a silent "errors: {...}" round trip.
const NUMBER_FIELDS: Record<string, { min?: number; integer?: boolean }> = {
  temperature: { min: 0 },
  max_loops: { min: 1, integer: true },
  max_output_chars: { min: 1, integer: true },
  num_ctx: { min: 1, integer: true },
  num_gpu: { min: 0, integer: true },
  num_thread: { min: 0, integer: true },
  num_batch: { min: 1, integer: true },
};

function validateField(key: string, value: FieldValue): string | null {
  const rule = NUMBER_FIELDS[key];
  if (!rule || typeof value !== "string") return null;
  if (value.trim() === "") return "required";
  const n = Number(value);
  if (Number.isNaN(n)) return "must be a number";
  if (rule.integer && !Number.isInteger(n)) return "must be a whole number";
  if (rule.min !== undefined && n < rule.min) return `must be ≥ ${rule.min}`;
  return null;
}

// How long to wait for a config_updated reply before treating the save as
// failed — the socket can silently drop a frame (e.g. reconnect mid-flight)
// and "saving…" should never hang forever with no way out for the user.
const SAVE_TIMEOUT_MS = 8000;

export function SettingsDrawer(props: Props) {
  const {
    connected, config, configUpdateResult, getConfig, updateConfig,
    personas, personaAddResult, listPersonas, addPersona,
    plugins, pluginToggleResult, listPlugins, togglePlugin,
    toolManuals, manualAddResult, listToolManuals, addToolManual,
  } = props;

  const [open, setOpen] = useState(false);
  const loadedRef = useRef(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const saveTimeoutRef = useRef<number | null>(null);
  const hoverCloseTimeoutRef = useRef<number | null>(null);

  const [liveForm, setLiveForm] = useState<Record<string, FieldValue>>({});
  const [restartForm, setRestartForm] = useState<Record<string, FieldValue>>({});
  const [personaSelect, setPersonaSelect] = useState("");
  const [showPersonaForm, setShowPersonaForm] = useState(false);
  const [newPersonaName, setNewPersonaName] = useState("");
  const [newPersonaContent, setNewPersonaContent] = useState("");
  const [showManualForm, setShowManualForm] = useState(false);
  const [newManualName, setNewManualName] = useState("");
  const [newManualContent, setNewManualContent] = useState("");
  const [drawerMsg, setDrawerMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [saving, setSaving] = useState(false);

  // Field-level validation errors, keyed by field name, recomputed whenever
  // either form changes. Save is disabled while any are present.
  const fieldErrors: Record<string, string> = {};
  for (const [k, v] of Object.entries(liveForm)) {
    const err = validateField(k, v);
    if (err) fieldErrors[k] = err;
  }
  for (const [k, v] of Object.entries(restartForm)) {
    const err = validateField(k, v);
    if (err) fieldErrors[k] = err;
  }
  const hasErrors = Object.keys(fieldErrors).length > 0;

  // Seed the editable form whenever a fresh config_state arrives.
  useEffect(() => {
    if (!config) return;
    const live: Record<string, FieldValue> = {};
    for (const f of LIVE_FIELD_META) {
      const v = config.live[f.key as keyof typeof config.live];
      live[f.key] = f.key === "scope" && Array.isArray(v) ? v.join(", ") : ((v ?? "") as FieldValue);
    }
    setLiveForm(live);
    const restart: Record<string, FieldValue> = {};
    for (const f of RESTART_FIELD_META) {
      const v = config.restart[f.key as keyof typeof config.restart];
      restart[f.key] = f.type === "checkbox" ? Boolean(v) : ((v ?? "") as FieldValue);
    }
    setRestartForm(restart);
  }, [config]);

  useEffect(() => {
    if (config?.live.persona) setPersonaSelect(config.live.persona);
  }, [config]);

  useEffect(() => {
    if (personaAddResult) {
      setDrawerMsg({
        text: personaAddResult.ok
          ? `persona '${personaAddResult.name}' created`
          : personaAddResult.error ?? "failed to create persona",
        ok: !!personaAddResult.ok,
      });
      if (personaAddResult.ok) {
        setNewPersonaName("");
        setNewPersonaContent("");
        setShowPersonaForm(false);
      }
    }
  }, [personaAddResult]);

  useEffect(() => {
    if (pluginToggleResult) {
      setDrawerMsg({
        text: pluginToggleResult.ok
          ? `${pluginToggleResult.name} ${pluginToggleResult.enabled ? "enabled" : "disabled"}`
          : pluginToggleResult.error ?? "toggle failed",
        ok: !!pluginToggleResult.ok,
      });
    }
  }, [pluginToggleResult]);

  useEffect(() => {
    if (manualAddResult) {
      setDrawerMsg({
        text: manualAddResult.ok
          ? `manual '${manualAddResult.name}' created`
          : manualAddResult.error ?? "failed to create manual",
        ok: !!manualAddResult.ok,
      });
      if (manualAddResult.ok) {
        setNewManualName("");
        setNewManualContent("");
        setShowManualForm(false);
      }
    }
  }, [manualAddResult]);

  useEffect(() => {
    if (configUpdateResult) {
      if (saveTimeoutRef.current) {
        window.clearTimeout(saveTimeoutRef.current);
        saveTimeoutRef.current = null;
      }
      setSaving(false);
      const parts: string[] = [];
      const applied = Object.keys(configUpdateResult.applied);
      const restart = Object.keys(configUpdateResult.queued_for_restart);
      const errors = Object.keys(configUpdateResult.errors);
      if (applied.length) parts.push(`applied: ${applied.join(", ")}`);
      if (restart.length) parts.push(`needs restart: ${restart.join(", ")}`);
      if (errors.length) parts.push(`errors: ${errors.join(", ")}`);
      setDrawerMsg({ text: parts.join("  ·  ") || "no changes", ok: errors.length === 0 });
    }
  }, [configUpdateResult]);

  // Click-outside and Escape close the drawer — a hover-only trigger isn't
  // reachable on touch devices at all, and it's finicky even with a mouse
  // once you're interacting with fields inside it.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    return () => {
      if (saveTimeoutRef.current) window.clearTimeout(saveTimeoutRef.current);
      if (hoverCloseTimeoutRef.current) window.clearTimeout(hoverCloseTimeoutRef.current);
    };
  }, []);

  function loadDrawerDataOnce() {
    if (!loadedRef.current && connected) {
      loadedRef.current = true;
      getConfig();
      listPersonas();
      listPlugins();
      listToolManuals();
    }
  }

  function toggleDrawer() {
    setOpen((wasOpen) => {
      const next = !wasOpen;
      if (next) loadDrawerDataOnce();
      return next;
    });
  }

  // [HOVER-1] Opens on hover of the icon (or the panel itself, once open —
  // they're both inside rootRef, and mouseenter/mouseleave are DOM-tree-
  // based, not paint-based, so hovering the panel keeps this "entered"
  // even though it's absolutely positioned below the icon with a small
  // visual gap). A short close delay absorbs the moment the cursor is
  // briefly over neither element while crossing that gap — without it,
  // a straight-line mouse path from icon to panel can clip the gap and
  // slam the panel shut before you get there.
  //
  // Click is kept as a second way to open/close — hover-only triggers are
  // unreachable for touch and awkward for keyboard users, and clicking
  // the gear is a natural fallback either way.
  function handleMouseEnter() {
    if (hoverCloseTimeoutRef.current) {
      window.clearTimeout(hoverCloseTimeoutRef.current);
      hoverCloseTimeoutRef.current = null;
    }
    loadDrawerDataOnce();
    setOpen(true);
  }

  function handleMouseLeave() {
    if (hoverCloseTimeoutRef.current) window.clearTimeout(hoverCloseTimeoutRef.current);
    hoverCloseTimeoutRef.current = window.setTimeout(() => {
      setOpen(false);
      hoverCloseTimeoutRef.current = null;
    }, 250);
  }

  function save() {
    if (hasErrors) {
      setDrawerMsg({ text: "fix the highlighted fields before saving", ok: false });
      return;
    }
    const changes: Record<string, unknown> = { ...liveForm, ...restartForm, persona: personaSelect };
    updateConfig(changes);
    setSaving(true);
    setDrawerMsg({ text: "saving…", ok: true });
    if (saveTimeoutRef.current) window.clearTimeout(saveTimeoutRef.current);
    saveTimeoutRef.current = window.setTimeout(() => {
      setSaving(false);
      setDrawerMsg({ text: "no response from server — check your connection and try again", ok: false });
      saveTimeoutRef.current = null;
    }, SAVE_TIMEOUT_MS);
  }

  function submitNewPersona() {
    const name = newPersonaName.trim();
    if (!name) return;
    addPersona(name, newPersonaContent);
  }

  function submitNewManual() {
    const name = newManualName.trim();
    if (!name || !newManualContent.trim()) return;
    addToolManual(name, newManualContent);
  }

  return (
    <div className="relative" ref={rootRef} onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}>
      <motion.button
        type="button"
        onClick={toggleDrawer}
        title="settings"
        aria-label="settings"
        aria-expanded={open}
        aria-haspopup="dialog"
        animate={{ rotate: open ? 45 : 0 }}
        transition={{ type: "spring", stiffness: 300, damping: 20 }}
        whileTap={{ scale: 0.88 }}
        className={`ml-3 flex h-6 w-6 items-center justify-center rounded text-[1.1em] transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-gold ${
          open ? "text-gold" : "text-goldDim hover:text-gold"
        }`}
      >
        ⚙
      </motion.button>

      <AnimatePresence>
        {open && (
          <motion.div
            role="dialog"
            aria-label="Settings"
            initial={{ opacity: 0, scale: 0.96, y: -8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: -8 }}
            transition={{ type: "spring", stiffness: 380, damping: 28 }}
            className="absolute right-0 top-7 z-50 max-h-[70vh] w-80 overflow-y-auto scroll-thin rounded-lg border border-goldDim bg-panel p-3.5 text-[13px] text-cream shadow-2xl"
          >
          <SectionTitle first>live — applies instantly</SectionTitle>
          {LIVE_FIELD_META.map((f) => (
            <FieldRow
              key={f.key}
              label={f.label}
              type={f.type}
              value={liveForm[f.key] ?? ""}
              error={fieldErrors[f.key]}
              onChange={(v) => setLiveForm((s) => ({ ...s, [f.key]: v }))}
            />
          ))}
          <div className="flex items-center justify-between gap-2 py-1">
            <label className="flex-1 text-[0.85em] text-muted">persona</label>
            <select
              value={personaSelect}
              onChange={(e) => setPersonaSelect(e.target.value)}
              className="w-[130px] rounded border border-goldDim bg-ink px-1.5 py-0.5 text-[0.85em] text-cream"
            >
              {personas.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center justify-between py-1">
            <span />
            <motion.button
              type="button"
              onClick={() => setShowPersonaForm((s) => !s)}
              whileTap={{ scale: 0.92 }}
              className="rounded border border-goldDim px-2.5 py-0.5 text-[0.8em] hover:border-gold"
            >
              + new persona
            </motion.button>
          </div>
          {showPersonaForm && (
            <div className="my-1 rounded-md border border-goldDim bg-ink p-2">
              <input
                type="text"
                placeholder="name (e.g. sysadmin)"
                value={newPersonaName}
                onChange={(e) => setNewPersonaName(e.target.value)}
                className="mb-1.5 w-full rounded border border-goldDim bg-panel px-1.5 py-1 text-[0.85em] text-cream"
              />
              <textarea
                placeholder="system prompt (leave blank for a starter template)"
                rows={4}
                value={newPersonaContent}
                onChange={(e) => setNewPersonaContent(e.target.value)}
                className="mb-1.5 w-full rounded border border-goldDim bg-panel px-1.5 py-1 text-[0.85em] text-cream"
              />
              <motion.button
                type="button"
                onClick={submitNewPersona}
                whileTap={{ scale: 0.92 }}
                className="rounded border border-goldDim px-2.5 py-0.5 text-[0.8em] hover:border-gold"
              >
                create
              </motion.button>
            </div>
          )}

          <SectionTitle>requires restart</SectionTitle>
          {RESTART_FIELD_META.map((f) => (
            <FieldRow
              key={f.key}
              label={f.label}
              type={f.type}
              restart
              value={restartForm[f.key] ?? (f.type === "checkbox" ? false : "")}
              error={fieldErrors[f.key]}
              onChange={(v) => setRestartForm((s) => ({ ...s, [f.key]: v }))}
            />
          ))}

          <SectionTitle>plugins</SectionTitle>
          {plugins.length === 0 ? (
            <div className="py-0.5 text-[0.85em] text-muted">no plugins found</div>
          ) : (
            plugins.map((p) => {
              const label = Array.isArray(p.persona) ? p.persona.join(", ") : p.persona;
              return (
                <div
                  key={p.name}
                  className="flex items-center justify-between gap-2 border-b border-goldDim/25 py-1.5"
                >
                  <div className={p.enabled === false ? "text-muted line-through" : ""}>
                    <div className="text-[0.85em] text-cream">{p.name}</div>
                    <div className="text-[0.75em] text-muted">
                      {p.error ? `error: ${p.error}` : `${label} · ${(p.tools ?? []).length} tool(s)`}
                    </div>
                  </div>
                  {!p.error && (
                    <ToggleSwitch
                      checked={p.enabled !== false}
                      onChange={(checked) => togglePlugin(p.name, checked)}
                    />
                  )}
                </div>
              );
            })
          )}

          <SectionTitle>tool manuals</SectionTitle>
          {toolManuals.length === 0 ? (
            <div className="py-0.5 text-[0.85em] text-muted">no manuals yet</div>
          ) : (
            toolManuals.map((name) => (
              <div key={name} className="py-0.5 text-[0.85em] text-muted">
                {name}
              </div>
            ))
          )}
          {showManualForm && (
            <div className="my-1.5 rounded-md border border-goldDim bg-ink p-2">
              <input
                type="text"
                placeholder="tool name (e.g. nmap)"
                value={newManualName}
                onChange={(e) => setNewManualName(e.target.value)}
                className="mb-1.5 w-full rounded border border-goldDim bg-panel px-1.5 py-1 text-[0.85em] text-cream"
              />
              <textarea
                placeholder="manual content (usage, params, known failures)"
                rows={4}
                value={newManualContent}
                onChange={(e) => setNewManualContent(e.target.value)}
                className="mb-1.5 w-full rounded border border-goldDim bg-panel px-1.5 py-1 text-[0.85em] text-cream"
              />
              <motion.button
                type="button"
                onClick={submitNewManual}
                whileTap={{ scale: 0.92 }}
                className="rounded border border-goldDim px-2.5 py-0.5 text-[0.8em] hover:border-gold"
              >
                create
              </motion.button>
            </div>
          )}
          <motion.button
            type="button"
            onClick={() => setShowManualForm((s) => !s)}
            whileTap={{ scale: 0.92 }}
            className="rounded border border-goldDim px-2.5 py-0.5 text-[0.8em] hover:border-gold"
          >
            + new manual
          </motion.button>

          <div className="mt-3 flex items-center gap-2.5 border-t border-goldDim pt-2.5">
            <motion.button
              type="button"
              onClick={save}
              disabled={saving || hasErrors || !connected}
              whileTap={!(saving || hasErrors || !connected) ? { scale: 0.94 } : undefined}
              whileHover={!(saving || hasErrors || !connected) ? { scale: 1.03 } : undefined}
              className="rounded bg-gold px-4 py-1 text-[0.85em] font-bold text-ink transition-colors hover:bg-goldDim disabled:cursor-not-allowed disabled:bg-goldDim/40 disabled:text-muted"
            >
              {saving ? "Saving…" : "Save"}
            </motion.button>
            {!connected && <span className="text-[0.8em] text-rust">disconnected</span>}
            {/* [ANIM-1] Pop-in confirmation instead of a plain color-swapped
                span — keyed on the message text so each new result gets its
                own fresh entrance, distinct from the previous one. */}
            <AnimatePresence mode="wait">
              {drawerMsg && (
                <motion.span
                  key={drawerMsg.text}
                  initial={{ opacity: 0, scale: 0.85 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.85 }}
                  transition={{ type: "spring", stiffness: 400, damping: 25 }}
                  className={`text-[0.8em] ${drawerMsg.ok ? "text-sage" : "text-rust"}`}
                >
                  {drawerMsg.text}
                </motion.span>
              )}
            </AnimatePresence>
          </div>
        </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function SectionTitle({ children, first }: { children: ReactNode; first?: boolean }) {
  return (
    <div className={`text-[0.75em] uppercase tracking-wide text-gold ${first ? "mt-0" : "mt-2.5"} mb-1.5`}>
      {children}
    </div>
  );
}

function FieldRow({
  label,
  type,
  value,
  onChange,
  restart,
  error,
}: {
  label: string;
  type: "text" | "number" | "checkbox";
  value: FieldValue;
  onChange: (v: FieldValue) => void;
  restart?: boolean;
  error?: string;
}) {
  return (
    <div className="py-1">
      <div className="flex items-center justify-between gap-2">
        <label className={`flex-1 text-[0.85em] ${restart ? "text-amber" : "text-muted"}`}>{label}</label>
        {type === "checkbox" ? (
          <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        ) : (
          <input
            type={type === "number" ? "text" : type}
            inputMode={type === "number" ? "decimal" : undefined}
            value={String(value)}
            onChange={(e) => onChange(e.target.value)}
            aria-invalid={!!error}
            className={`w-[130px] rounded border bg-ink px-1.5 py-0.5 text-[0.85em] text-cream focus:outline-none ${
              error ? "border-rust" : "border-goldDim"
            }`}
          />
        )}
      </div>
      {error && <div className="mt-0.5 text-right text-[0.72em] text-rust">{error}</div>}
    </div>
  );
}

function ToggleSwitch({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="relative inline-block h-[18px] w-[34px] shrink-0 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="peer absolute h-0 w-0 opacity-0"
      />
      <span className="absolute inset-0 rounded-full border border-goldDim bg-ink transition-colors peer-checked:border-sage peer-checked:bg-sage/20" />
      <motion.span
        className="pointer-events-none absolute top-0.5 h-3 w-3 rounded-full"
        animate={{ x: checked ? 18 : 2, backgroundColor: checked ? "#7fa66b" : "#8f887c" }}
        transition={{ type: "spring", stiffness: 500, damping: 30 }}
      />
    </label>
  );
}