const API = '/api';

export async function fetchAgents(): Promise<Response> {
  return fetch(`${API}/agents`);
}

export async function sendDemoMessage(messageId: string): Promise<Response> {
  return fetch(`${API}/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: messageId }),
  });
}

export async function fetchAuditLog(): Promise<Response> {
  return fetch(`${API}/audit`);
}