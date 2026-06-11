import { Fragment } from "react";

export type GmailCandidateLine = {
  index: string;
  name: string;
  sender: string;
  count: string;
  subject: string;
  rationale?: string;
};

export function ChatMessageContent(props: { content: string }) {
  const gmailCandidateMessage = parseGmailCandidateMessage(props.content);
  if (gmailCandidateMessage) {
    return (
      <div className="gmail-candidate-message">
        <p>{gmailCandidateMessage.intro}</p>
        <ol className="gmail-candidate-list">
          {gmailCandidateMessage.candidates.map((candidate) => (
            <li key={`${candidate.index}-${candidate.sender}`}>
              <div>
                <strong>{candidate.name}</strong>
                <span>{candidate.sender}</span>
              </div>
              <small>{candidate.count} found · Latest: {candidate.subject}</small>
              {candidate.rationale ? <small>{candidate.rationale}</small> : null}
            </li>
          ))}
        </ol>
        <p>{gmailCandidateMessage.prompt}</p>
      </div>
    );
  }
  return (
    <>
      {props.content.split("\n").map((line, index) => (
        <Fragment key={`${index}-${line.slice(0, 16)}`}>
          {index > 0 ? <br /> : null}
          {line}
        </Fragment>
      ))}
    </>
  );
}

function parseGmailCandidateMessage(content: string): { intro: string; candidates: GmailCandidateLine[]; prompt: string } | null {
  const lines = content.split("\n").map((line) => line.trim()).filter(Boolean);
  const firstCandidateIndex = lines.findIndex((line) => /^\d+\.\s/.test(line));
  if (firstCandidateIndex < 1) return null;
  const candidates: GmailCandidateLine[] = [];
  let promptStart = lines.length;
  for (let index = firstCandidateIndex; index < lines.length; index += 1) {
    const match = lines[index].match(
      /^(\d+)\.\s+(.+?)\s+<([^>]+)>\s+\((\d+)\s+found;\s+latest subject:\s+(.+?)\)(?:\s+[—-]\s+(.+))?$/i,
    );
    if (!match) {
      promptStart = index;
      break;
    }
    candidates.push({
      index: match[1],
      name: match[2],
      sender: match[3],
      count: match[4],
      subject: match[5],
      rationale: match[6],
    });
  }
  if (!candidates.length || !lines[0].includes("found newsletter candidates")) return null;
  return {
    intro: lines.slice(0, firstCandidateIndex).join(" "),
    candidates,
    prompt: lines.slice(promptStart).join(" "),
  };
}
