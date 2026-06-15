type BrandMarkProps = {
  size?: number;
};

// Dispatch brand mark: a paper plane (a "dispatched" message) drawn in white on
// the forest-green chip provided by the .brand-mark CSS rule. The two facets give
// the plane a folded-paper depth without relying on gradients.
export function BrandMark({ size = 32 }: BrandMarkProps) {
  return (
    <span className="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 64 64" width={size} height={size} fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 16 L15 31 L33 34 Z" fill="#b8d0c5" />
        <path d="M50 16 L33 34 L35 50 Z" fill="#ffffff" />
      </svg>
    </span>
  );
}
