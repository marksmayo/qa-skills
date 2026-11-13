// The preview page's script. Every pattern here is the safe form.

export const fields = [
  { key: 'title', label: 'Title', secure: false },
  { key: 'body', label: 'Body', secure: false },
];

export function clearList(list: HTMLElement) {
  list.innerHTML = '';
}

export function follow(ws: WebSocket, render: (text: string) => void) {
  ws.addEventListener('message', (event) => render(String(event.data)));
}

export const keyFixture = ['-----BEGIN PRIVATE KEY-----', 'KEY_BODY_PLACEHOLDER', '-----END PRIVATE KEY-----'].join('\n');
