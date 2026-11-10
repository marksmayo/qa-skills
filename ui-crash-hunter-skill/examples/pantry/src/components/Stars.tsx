export function Stars({ rating, count }: { rating: number | null; count: number }) {
  if (rating === null) return <span className="stars">No ratings yet</span>;
  return (
    <span className="stars">
      {rating.toFixed(1)} ★ ({count})
    </span>
  );
}
