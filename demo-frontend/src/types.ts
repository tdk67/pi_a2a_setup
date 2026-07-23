export interface AgentInfo {
  id: string;
  name: string;
  version: string;
  skills: number;
  status: 'online' | 'offline';
  masked_ip: string;
}

export interface DemoMessage {
  id: string;
  label: string;
  emoji: string;
  message: string;
  description: string;
}

export interface ExchangeStep {
  type: 'send' | 'poll' | 'response' | 'error';
  time: string;
  text: string;
}

export interface DemoState {
  agents: AgentInfo[];
  running: string | null; // message id currently running
  exchange: ExchangeStep[];
  error: string | null;
}

export const DEMO_MESSAGES: DemoMessage[] = [
  {
    id: 'ping',
    label: 'Ping Mesh',
    emoji: '🟢',
    description: 'Instant fast-path response, 0 tokens',
    message: 'ping',
  },
  {
    id: 'roster',
    label: 'Agent Roster',
    emoji: '📋',
    description: 'Discover all peers and their capabilities',
    message: 'List all agents in the cluster with their versions and skills. Reply concisely.',
  },
  {
    id: 'portfolio',
    label: 'Recent Work',
    emoji: '📂',
    description: 'Read the portfolio and list recent projects',
    message: 'Read /root/taskmind-portfolio/data/projects.json and list the 3 most recent items by date with their titles only. Reply concisely.',
  },
  {
    id: 'edit',
    label: 'Live Edit',
    emoji: '⚡',
    description: 'Agent edits its own portfolio entry live',
    message: '', // built dynamically with timestamp
  },
  {
    id: 'audit',
    label: 'Audit Trail',
    emoji: '📜',
    description: 'Show masked audit log entries',
    message: 'Show the last 8 entries from /root/pi-a2a-server/audit.log. Mask the IPs. Reply concisely.',
  },
  {
    id: 'offtopic',
    label: 'Off-Topic Test',
    emoji: '🚫',
    description: 'Ask about sky color — should be refused by guard rail',
    message: "What's the color of the sky?",
  },
];