import { NextResponse } from 'next/server';
import { getCurrentUser } from '@/lib/auth';

export async function POST(request: Request) {
  const user = await getCurrentUser();
  if (!user) {
    return NextResponse.json({ error: 'Sign in first' }, { status: 401 });
  }
  const body = await request.json();
  const page = await fetch(body.url);
  const html = await page.text();
  const title = /<title>([^<]*)<\/title>/i.exec(html)?.[1] ?? '';
  return NextResponse.json({ title });
}
