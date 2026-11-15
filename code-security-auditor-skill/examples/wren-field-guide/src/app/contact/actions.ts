'use server';

export async function sendSighting(form: FormData) {
  const note = String(form.get('note') ?? '').slice(0, 500);
  await fetch('https://hooks.example.com/sightings', { method: 'POST', body: note });
}
