import { put } from '@vercel/blob';
import { NextResponse } from 'next/server';
import { requireMember } from '@/lib/auth';

export async function POST(request: Request) {
  await requireMember();
  const form = await request.formData();
  const file = form.get('file') as File;
  const blob = await put(`attachments/${file.name}`, file, { access: 'public', contentType: file.type });
  return NextResponse.json({ url: blob.url });
}
