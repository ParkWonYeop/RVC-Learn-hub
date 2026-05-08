export function EmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="empty-state" role="status">
      <span aria-hidden="true">∅</span>
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  );
}
