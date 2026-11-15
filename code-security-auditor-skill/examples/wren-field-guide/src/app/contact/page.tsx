import { sendSighting } from './actions';

export default function Contact() {
  return (
    <form action={sendSighting}>
      <textarea name="note" />
      <button type="submit">Send</button>
    </form>
  );
}
