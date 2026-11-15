import { ImageResponse } from 'next/og';
import { BIRDS } from '../../data/birds';

export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

export default async function Image({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const bird = BIRDS.find((b) => b.id === id);
  return new ImageResponse(<div style={{ fontSize: 96 }}>{bird?.name ?? 'Bird'}</div>, size);
}
