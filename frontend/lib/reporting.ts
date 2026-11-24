type Member = { id: number; manager_agent_id?: number | null };

// An employee with no explicit manager follows the current lead (legacy and manual hires).
// Missing or circular links are unresolved and must not be drawn as valid reporting lines.
export function reportingPath<T extends Member>(members: readonly T[], leadId: number | null, id: number): T[] | null {
  const byId = new Map(members.map(member => [member.id, member]));
  const path: T[] = [];
  const seen = new Set<number>();
  let current = byId.get(id);
  while (current && !seen.has(current.id)) {
    path.push(current);
    if (current.id === leadId) return path.reverse();
    seen.add(current.id);
    const parent: number | null = current.manager_agent_id ?? leadId;
    current = parent === null ? undefined : byId.get(parent);
  }
  return null;
}
