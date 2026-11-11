# How to demo it

The page: https://clinic-assistant-mocha.vercel.app

## Before class
1. Open the page and ask one question, so the first request of the night is not the one the room watches. A serverless function that has been idle takes a moment longer to start.
2. Check https://clinic-assistant-mocha.vercel.app/health says `"status": "ok"`.

## The four questions (they are the four chips on the page)

1. **"Are you open on Saturdays?"** Point at, in order: the two probabilities from the judgment model and the thresholds they were compared to; the four passages with their similarity scores; the answer and its citations (click one). Then open "Show the exact prompt": *this is the entire trick. We hand the model the right pages and ask it to cite them.*
2. **"Can I do a telehealth visit for a refill, and what if I need to cancel it?"** The citations come from more than one section of the handbook. Retrieval found passages from different files and the model combined them.
3. **"What's a normal dose of ibuprofen for a 10 year old?"** Declined. *The model that writes answers never saw this question. A different model gave a probability that it belongs in a clinic handbook, and code refused to generate.* Notice how fast it was: nothing was generated.
4. **"My father has crushing chest pain and his left arm is numb"** The fixed 911 message. *No model wrote that. Code did.* A wrong answer here is the failure this whole design exists to prevent, which is why the emergency threshold is 0.35 and not 0.5.

Close with: *by March, you will have built one of these yourself.*

## Good follow-ups if someone asks
- **"What if it gets it wrong?"** Ask "Do you offer dental cleanings?". It sounds like a clinic question, so the scope judgment lets it through, but nothing relevant is retrieved and the similarity floor declines it. Three layers, each catching what the others miss (DECISIONS D28).
- **"How much does this cost?"** About two hundredths of a cent per answered question (measured: $0.00016 on average). Declines and emergencies cost almost nothing because no answer is generated.
- **"Can I use it?"** Yes: the page is open, and every student gets a token for the class endpoint.

## If something fails live
- **A slow first answer:** it is a cold start. Ask again.
- **"Too many requests":** the page allows 10 questions a minute per visitor. Wait a minute.
- **An error that persists:** check `/health`, then switch to the recorded capture of the same four questions in the session deck. Do not debug in front of the room.
- **To stop all traffic:** set `kill_switch` to `true` in the `app_settings` table.
