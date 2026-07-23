import { useState, useEffect, useCallback } from 'react';
import type { DemoState, ExchangeStep } from './types';
import { DEMO_MESSAGES } from './types';
import { fetchAgents, sendDemoMessage } from './api';
import { Activity, Server, Zap, Shield, Terminal, Loader2, AlertTriangle, CheckCircle2, Clock, ArrowRight, RefreshCw, Radio } from 'lucide-react';

const AGENT_TARGETS = [
  { id: 'pisti', label: 'Pisti', subtitle: 'Coordinator', color: 'emerald' },
  { id: 'sisi', label: 'Sisi', subtitle: 'Database', color: 'blue' },
  { id: 'nori', label: 'Nori', subtitle: 'Automation', color: 'purple' },
];

const colorMap: Record<string, { bg: string; text: string; border: string; glow: string }> = {
  emerald: { bg: 'bg-emerald-500/10', text: 'text-emerald-400', border: 'border-emerald-500/30', glow: 'bg-emerald-400' },
  blue: { bg: 'bg-blue-500/10', text: 'text-blue-400', border: 'border-blue-500/30', glow: 'bg-blue-400' },
  purple: { bg: 'bg-purple-500/10', text: 'text-purple-400', border: 'border-purple-500/30', glow: 'bg-purple-400' },
};

export default function App() {
  const [state, setState] = useState<DemoState>({
    agents: [],
    running: null,
    exchange: [],
    error: null,
  });
  const [agentError, setAgentError] = useState<string | null>(null);
  const [target, setTarget] = useState('pisti');

  const loadAgents = useCallback(async () => {
    try {
      const res = await fetchAgents();
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setState(s => ({ ...s, agents: data.agents }));
      setAgentError(null);
    } catch (e: any) {
      setAgentError(e.message || 'Failed to load agents');
    }
  }, []);

  useEffect(() => { loadAgents(); const i = setInterval(loadAgents, 15000); return () => clearInterval(i); }, [loadAgents]);

  const addStep = useCallback((step: ExchangeStep) => {
    setState(s => ({ ...s, exchange: [...s.exchange, step] }));
  }, []);

  const runDemo = useCallback(async (msg: typeof DEMO_MESSAGES[0]) => {
    if (state.running) return;
    const targetAgent = AGENT_TARGETS.find(a => a.id === target)!;
    
    setState(s => ({ ...s, running: msg.id, error: null, exchange: [] }));

    const startTime = new Date().toLocaleTimeString();
    addStep({ type: 'send', time: startTime, text: `→ Sending "${msg.label}" to ${targetAgent.label} (${targetAgent.subtitle})...` });

    try {
      const res = await sendDemoMessage(msg.id, target);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        throw new Error(err.error || err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();

      if (data.steps) {
        for (const step of data.steps) {
          const t = new Date().toLocaleTimeString();
          addStep({ type: 'poll', time: t, text: `  ⏳ Poll ${step.poll}: ${step.state} (${step.delay}s)` });
          await new Promise(r => setTimeout(r, 300));
        }
      }

      const endTime = new Date().toLocaleTimeString();
      const duration = data.duration_ms ? `${(data.duration_ms / 1000).toFixed(1)}s` : '?';
      const routedVia = data.routed_via ? ` [routed via ${data.routed_via}]` : '';
      addStep({ type: 'response', time: endTime, text: `← ${targetAgent.label}${routedVia} (${duration}):\n${data.response || '(empty)'}` });
    } catch (e: any) {
      const t = new Date().toLocaleTimeString();
      addStep({ type: 'error', time: t, text: `✗ Error from ${targetAgent.label}: ${e.message}` });
    } finally {
      setState(s => ({ ...s, running: null }));
    }
  }, [state.running, addStep, target]);

  return (
    <div className="min-h-screen bg-zinc-950">
      <header className="border-b border-zinc-800 bg-zinc-900/50 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-emerald-500/20 flex items-center justify-center">
              <Activity className="w-5 h-5 text-emerald-400" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-white">Pi A2A Agent Mesh</h1>
              <p className="text-xs text-zinc-500">Live Demo</p>
            </div>
          </div>
          <div className="flex items-center gap-4 text-xs text-zinc-500">
            <div className="flex items-center gap-1.5">
              <Shield className="w-3.5 h-3.5 text-emerald-500" />
              <span>5-layer security</span>
            </div>
            <div className="flex items-center gap-1.5">
              <Zap className="w-3.5 h-3.5 text-amber-500" />
              <span>Dual-model</span>
            </div>
            <button onClick={loadAgents} className="flex items-center gap-1 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 transition-colors">
              <RefreshCw className="w-3 h-3" /> Refresh
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8 space-y-8">
        {/* Agent Status */}
        <section>
          <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-4 flex items-center gap-2">
            <Server className="w-4 h-4" /> Agent Mesh Status
          </h2>
          {agentError ? (
            <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-center gap-3 text-red-400 text-sm">
              <AlertTriangle className="w-4 h-4 flex-shrink-0" />
              {agentError}
              <button onClick={loadAgents} className="ml-auto text-xs underline hover:text-red-300">Retry</button>
            </div>
          ) : state.agents.length === 0 ? (
            <div className="grid grid-cols-3 gap-4">
              {[1, 2, 3].map(i => (
                <div key={i} className="bg-zinc-900 border border-zinc-800 rounded-xl p-5 animate-pulse">
                  <div className="h-4 bg-zinc-800 rounded w-20 mb-3" />
                  <div className="h-3 bg-zinc-800 rounded w-16 mb-2" />
                  <div className="h-3 bg-zinc-800 rounded w-24" />
                </div>
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-4">
              {state.agents.map(agent => {
                const c = AGENT_TARGETS.find(a => a.label === agent.name);
                const col = colorMap[c?.color || 'emerald'];
                return (
                  <div key={agent.id} className={`bg-zinc-900 border rounded-xl p-5 transition-all ${agent.status === 'online' ? col.border : 'border-red-500/30'}`}>
                    <div className="flex items-center justify-between mb-3">
                      <h3 className="font-semibold text-white">{agent.name}</h3>
                      <span className={`flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full ${agent.status === 'online' ? `${col.bg} ${col.text}` : 'bg-red-500/10 text-red-400'}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${agent.status === 'online' ? `${col.glow} animate-pulse-glow` : 'bg-red-400'}`} />
                        {agent.status}
                      </span>
                    </div>
                    <div className="space-y-1 text-sm text-zinc-400">
                      <div className="flex justify-between"><span>Version</span><span className="text-zinc-300 font-mono">{agent.version}</span></div>
                      <div className="flex justify-between"><span>Skills</span><span className="text-zinc-300">{agent.skills}</span></div>
                      <div className="flex justify-between"><span>Address</span><span className="text-zinc-500 font-mono text-xs">{agent.masked_ip}</span></div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        {/* Target selector */}
        <section>
          <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-4 flex items-center gap-2">
            <Radio className="w-4 h-4" /> Select Target Agent
          </h2>
          <div className="flex gap-3">
            {AGENT_TARGETS.map(a => {
              const col = colorMap[a.color];
              const active = target === a.id;
              return (
                <button
                  key={a.id}
                  onClick={() => setTarget(a.id)}
                  disabled={state.running !== null}
                  className={`flex-1 rounded-xl p-4 text-left border transition-all ${active ? `${col.border} ${col.bg}` : 'border-zinc-800 bg-zinc-900 hover:border-zinc-600'} disabled:opacity-50`}
                >
                  <div className={`font-semibold text-sm ${active ? col.text : 'text-zinc-300'}`}>{a.label}</div>
                  <div className="text-xs text-zinc-500 mt-0.5">{a.subtitle}</div>
                  {active && <div className={`mt-2 text-xs ${col.text}`}>● Selected — messages go here</div>}
                </button>
              );
            })}
          </div>
        </section>

        {/* Demo Messages */}
        <section>
          <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-4 flex items-center gap-2">
            <Terminal className="w-4 h-4" /> Pre-Approved Messages
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {DEMO_MESSAGES.map(msg => (
              <button
                key={msg.id}
                onClick={() => runDemo(msg)}
                disabled={state.running !== null}
                className={`bg-zinc-900 border rounded-xl p-4 text-left transition-all ${state.running === msg.id ? 'border-amber-500/50 bg-amber-500/5' : 'border-zinc-800 hover:border-zinc-600 hover:bg-zinc-800/50'} disabled:opacity-50 disabled:cursor-not-allowed`}
              >
                <div className="text-2xl mb-2">{msg.emoji}</div>
                <div className="font-medium text-white text-sm">{msg.label}</div>
                <div className="text-xs text-zinc-500 mt-1 leading-relaxed">{msg.description}</div>
                {state.running === msg.id && (
                  <div className="flex items-center gap-1.5 mt-2 text-amber-400 text-xs">
                    <Loader2 className="w-3 h-3 animate-spin" /> Running...
                  </div>
                )}
              </button>
            ))}
          </div>
        </section>

        {/* Live Exchange */}
        <section>
          <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-4 flex items-center gap-2">
            <Activity className="w-4 h-4" /> Live Exchange
          </h2>
          <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-5 min-h-[200px] max-h-[400px] overflow-y-auto">
            {state.exchange.length === 0 ? (
              <div className="flex items-center justify-center h-40 text-zinc-600 text-sm">
                <div className="text-center">
                  <ArrowRight className="w-8 h-8 mx-auto mb-2 opacity-50" />
                  <p>Select a target agent above, then click a message to see live communication</p>
                </div>
              </div>
            ) : (
              <div className="space-y-1">
                {state.exchange.map((step, i) => (
                  <div key={i} className={`log-line flex gap-2 ${step.type === 'error' ? 'text-red-400' : step.type === 'response' ? 'text-emerald-300' : step.type === 'poll' ? 'text-amber-300/70' : 'text-zinc-300'}`}>
                    <span className="text-zinc-600 flex-shrink-0">
                      {step.type === 'send' ? <ArrowRight className="w-3.5 h-3.5 mt-0.5" /> :
                       step.type === 'response' ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5" /> :
                       step.type === 'error' ? <AlertTriangle className="w-3.5 h-3.5 mt-0.5" /> :
                       <Clock className="w-3.5 h-3.5 mt-0.5" />}
                    </span>
                    <span className="text-zinc-600 flex-shrink-0">{step.time}</span>
                    <span className="whitespace-pre-wrap">{step.text}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        <footer className="text-center text-xs text-zinc-600 pt-4 border-t border-zinc-800">
          <p>Pi A2A Agent Mesh · <a href="https://github.com/tdk67/pi_a2a_setup" className="text-zinc-500 hover:text-zinc-300 underline">github.com/tdk67/pi_a2a_setup</a></p>
          <p className="mt-1">All tokens and IPs are masked. Messages are pre-approved. Real agent responses only.</p>
        </footer>
      </main>
    </div>
  );
}