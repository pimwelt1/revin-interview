#### /call-ended in telephony is too complex for nothing



CLEAN THE CODE AND HAVE A CLEAR UNDERSTANDING OF WHAT IS DOING WHAT --> own it
Be ready to debug
Does it start if it shouldn't

TO fix
don't give the dates in EDT
Can I modify on my previous phone call
what happens if booked at the same time/it fails? --> it should check again and try again
It should prompt start with --> sorry you missed us. let me gather your info and we'll schedule a service call.
it should ask for the contact details last --> what happens, 
what is the chat.py??? is it for manual tests?

storage creates 2 sources of truth --> bad
good to have something that keeps in mind the infos to edit them
the language shouldn't change mid conversation

what is the format of the message returned by twilio??

1)  the changing of language in start turn doesn't make sense


Look at question and answer and knowledge, update knowledge


prompt --> start turn (language check, confirmation check if a question is pending, use caller_agreed???, update the phone confirm to True ????WTF, )


--> run agent (
  look for the last call

  look for only reply


  foop 6 times (wtf)
  look for all the tools call ???

  return a clarify if nothing happens
)


PB: check nearby availabilities first and propose closer slots.

