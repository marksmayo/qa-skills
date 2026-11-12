'use server';

import { prisma } from '@/lib/prisma';
import { requireAdmin } from '@/lib/auth';

export async function renameTeam(teamId: string, name: string) {
  await prisma.team.update({ where: { id: teamId }, data: { name } });
}

export async function deleteTeam(teamId: string) {
  await requireAdmin();
  await prisma.team.delete({ where: { id: teamId } });
}
