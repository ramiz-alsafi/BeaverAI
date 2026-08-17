import { lazy, Suspense, useState } from "react";
import { motion } from "framer-motion";
import { ChatLog } from "./components/ChatLog";
import { Composer } from "./components/Composer";
import { Sidebar } from "./components/Sidebar";
import { StatusBanner } from "./components/StatusBanner";
import { TokenBar } from "./components/TokenBar";
import { useBeaverSocket } from "./hooks/useBeaverSocket";

// [PERF] Code-split out — hover-triggered (settings gear), not needed on
// first paint, and it's the single biggest component in the app.
const SettingsDrawer = lazy(() =>
  import("./components/SettingsDrawer").then((m) => ({ default: m.SettingsDrawer }))
);

// [PERF]/[LOGS-1] Same reasoning as SettingsDrawer — click-triggered, not
// needed on first paint, and it pulls in prettyJson's parser too.
const ActivityPanel = lazy(() =>
  import("./components/ActivityPanel").then((m) => ({ default: m.ActivityPanel }))
);

export default function App() {
  const {
    connected, authError, setToken, log, turnInProgress, tokenUsage, status,
    send, sendCommand, stop,
    config, configUpdateResult, getConfig, updateConfig,
    personas, personaAddResult, listPersonas, addPersona,
    plugins, pluginToggleResult, listPlugins, togglePlugin,
    toolManuals, manualAddResult, listToolManuals, addToolManual,
    activeTools, memories, sidebarSessions, listActiveTools, listMemories, listSidebarSessions,
    agentLog, tracesLog, uvicornLog, tailLog, watchLog, unwatchLog,
  } = useBeaverSocket();

  const [tokenInput, setTokenInput] = useState("");
  // [SIDEBAR-1]/[LOGS-1] Two side panels, independent of each other and of
  // the settings drawer — sidebar slides in from the left (sessions/tools/
  // memory), activity slides in from the right edge of the chat column
  // (raw events/agent.log/traces/uvicorn). Nothing about the existing
  // chat/composer/settings layout changes when these are closed.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);

  return (
    <div className="relative flex h-screen flex-col overflow-hidden bg-ink">
      {/* [POLISH]/[ANIM-1] Layered background — a soft radial glow anchored
          top-left plus a faint sage glow bottom-right, now with a very slow
          breathing drift instead of sitting perfectly static. Deliberately
          understated — this should read as "alive" only if you happen to
          notice it, never as something competing for attention. */}
      <div className="pointer-events-none absolute inset-0 -z-10">
        <motion.div
          className="absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_20%_-10%,rgba(217,164,65,0.08),transparent)]"
          animate={{ opacity: [0.85, 1, 0.85] }}
          transition={{ duration: 12, repeat: Infinity, ease: "easeInOut" }}
        />
        <motion.div
          className="absolute inset-0 bg-[radial-gradient(ellipse_60%_40%_at_100%_100%,rgba(127,166,107,0.05),transparent)]"
          animate={{ opacity: [1, 0.7, 1] }}
          transition={{ duration: 16, repeat: Infinity, ease: "easeInOut" }}
        />
      </div>

      <header className="relative z-50 flex items-center justify-between border-b border-goldDim/20 bg-panel/80 px-4 py-2.5 backdrop-blur-sm">
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() => setSidebarOpen((s) => !s)}
            title="sessions / tools / memory"
            aria-label="Open sidebar"
            aria-expanded={sidebarOpen}
            className={`flex h-6 w-6 items-center justify-center rounded text-[1.05em] transition-colors ${
              sidebarOpen ? "text-gold" : "text-goldDim hover:text-gold"
            }`}
          >
            ☰
          </button>
          <span className="text-[1.05em] font-bold tracking-wide text-gold drop-shadow-[0_0_6px_rgba(217,164,65,0.35)]">
            BEAVER
          </span>
          <span className="hidden text-[0.78em] text-muted md:inline">
            riskwatchpro.online <span className="text-goldDim/50">·</span> Ramiz Alsafi
          </span>
        </div>

        <StatusBanner status={status} />

        <div className="flex items-center text-[0.85em] text-muted">
          <span className={`mr-1.5 inline-block h-2 w-2 rounded-full transition-colors ${connected ? "bg-sage" : authError ? "bg-gold" : "bg-rust"}`} />
          <span>{connected ? "connected" : authError ? "unauthorized" : "reconnecting…"}</span>
          <button
            type="button"
            onClick={() => setActivityOpen((s) => !s)}
            title="events / agent.log / traces / uvicorn"
            aria-label="Open activity panel"
            aria-expanded={activityOpen}
            className={`ml-3 flex h-6 w-6 items-center justify-center rounded text-[1.05em] transition-colors ${
              activityOpen ? "text-gold" : "text-goldDim hover:text-gold"
            }`}
          >
            ⌁
          </button>
          <Suspense fallback={<span className="ml-3 text-[1.1em] text-goldDim/40">⚙</span>}>
            <SettingsDrawer
              connected={connected}
              config={config}
              configUpdateResult={configUpdateResult}
              getConfig={getConfig}
              updateConfig={updateConfig}
              personas={personas}
              personaAddResult={personaAddResult}
              listPersonas={listPersonas}
              addPersona={addPersona}
              plugins={plugins}
              pluginToggleResult={pluginToggleResult}
              listPlugins={listPlugins}
              togglePlugin={togglePlugin}
              toolManuals={toolManuals}
              manualAddResult={manualAddResult}
              listToolManuals={listToolManuals}
              addToolManual={addToolManual}
            />
          </Suspense>
        </div>
      </header>

      {/* [SIDEBAR-1] Fixed-position overlay — self-contained, doesn't need
          to sit inside the flex row below. */}
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        currentThreadId={status?.thread_id}
        sendCommand={sendCommand}
        activeTools={activeTools}
        memories={memories}
        sidebarSessions={sidebarSessions}
        listActiveTools={listActiveTools}
        listMemories={listMemories}
        listSidebarSessions={listSidebarSessions}
      />

      <div className="flex flex-1 overflow-hidden">
        {authError ? (
          <div className="flex flex-1 items-center justify-center px-4">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const trimmed = tokenInput.trim();
                if (trimmed) setToken(trimmed);
              }}
              className="w-full max-w-sm rounded-lg border border-rust/40 bg-panel/80 p-6"
            >
              <p className="mb-1 text-center text-[1.05em] font-bold text-rust">unauthorized</p>
              <p className="mb-4 text-center text-[0.85em] text-muted">
                This Beaver instance requires a token. Enter the value of{" "}
                <code className="rounded bg-ink px-1 py-0.5 text-cream">BEAVER_WEB_TOKEN</code> from your{" "}
                <code className="rounded bg-ink px-1 py-0.5 text-cream">.env</code>.
              </p>
              <input
                type="password"
                autoFocus
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder="token"
                className="mb-3 w-full rounded border border-goldDim/30 bg-ink px-3 py-2 text-[0.9em] text-cream outline-none focus:border-gold"
              />
              <button
                type="submit"
                disabled={!tokenInput.trim()}
                className="w-full rounded bg-gold px-4 py-2 font-bold text-ink transition-colors hover:bg-goldDim disabled:opacity-40"
              >
                connect
              </button>
            </form>
          </div>
        ) : (
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            <ChatLog log={log} sendCommand={sendCommand} />

            <TokenBar count={tokenUsage.count} limit={tokenUsage.limit} />

            {/* [POLISH] Command hints — click to run instantly. Slash commands
                are safe/idempotent so there's no confirm-before-send step. */}
            <div className="mx-auto flex w-full max-w-3xl items-center gap-1.5 px-4 pb-1.5 pt-2 text-[11.5px] text-muted">
              <span>try:</span>
              <button
                onClick={() => sendCommand("/help")}
                className="rounded-full border border-goldDim/25 px-2.5 py-0.5 transition-colors hover:border-gold hover:text-cream"
              >
                /help
              </button>
              <button
                onClick={() => sendCommand("/models")}
                className="rounded-full border border-goldDim/25 px-2.5 py-0.5 transition-colors hover:border-gold hover:text-cream"
              >
                /models
              </button>
            </div>

            <div className="px-4 pb-4">
              <Composer turnInProgress={turnInProgress} onSend={send} onStop={stop} />
            </div>
          </div>
        )}

        {/* [LOGS-1] Sits beside the chat column, not over it — ActivityPanel
            animates its own width from 0, so this Suspense fallback only
            matters for the brief chunk-load window right after first click. */}
        <Suspense fallback={null}>
          <ActivityPanel
            log={log}
            agentLog={agentLog}
            tracesLog={tracesLog}
            uvicornLog={uvicornLog}
            open={activityOpen}
            onClose={() => setActivityOpen(false)}
            tailLog={tailLog}
            watchLog={watchLog}
            unwatchLog={unwatchLog}
          />
        </Suspense>
      </div>
    </div>
  );
}