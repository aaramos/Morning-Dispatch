export function SettingsErrorList(props: { errors: string[] }) {
  if (!props.errors.length) return null;
  return (
    <div className="settings-error-box" role="alert">
      <strong>Fix these values before saving:</strong>
      <ul>
        {props.errors.map((error) => (
          <li key={error}>{error}</li>
        ))}
      </ul>
    </div>
  );
}
