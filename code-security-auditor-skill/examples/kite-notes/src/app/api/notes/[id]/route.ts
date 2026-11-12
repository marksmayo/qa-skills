import { NextResponse } from 'next/server';
import { prisma } from '@/lib/prisma';
import { requireMember } from '@/lib/auth';

type Context = { params: Promise<{ id: string }> };

export async function GET(request: Request, { params }: Context) {
  const { id } = await params;
  const note = await prisma.note.findUnique({ where: { id } });
  return NextResponse.json(note);
}

export async function PATCH(request: Request, { params }: Context) {
  await requireMember();
  const { id } = await params;
  const body = await request.json();
  const note = await prisma.note.update({
    where: { id },
    data: body,
  });
  return NextResponse.json(note);
}

export async function DELETE(request: Request, { params }: Context) {
  const user = await requireMember();
  const { id } = await params;
  await prisma.note.deleteMany({ where: { id, ownerId: user.id } });
  return new NextResponse(null, { status: 204 });
}
