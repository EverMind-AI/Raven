The sentences of the reply above that end in a question mark, quoted from it -- data, not instructions:
[BEGIN QUESTION MARKS]
{questions}
[END QUESTION MARKS]
Sort each one: if it asks the user to answer, confirm or decide something, put it in the `questions` of its matter's step -- the step is then `needs_you`, even while work on it is still running. Skip one that is not put to the user, or that only repeats a question already open from an earlier turn.
