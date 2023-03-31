// Next.js API route support: https://nextjs.org/docs/api-routes/introduction
import type { NextApiRequest, NextApiResponse } from 'next'
const { Configuration, OpenAIApi } = require("openai");
const { encode, decode } = require('gpt-3-encoder')

const configuration = new Configuration({
  apiKey: process.env.OPENAI_API_KEY,
});
const openai = new OpenAIApi(configuration);

const handler = async (
  req: NextApiRequest,
  res: NextApiResponse
) => {
  const openAiMaxResponseTokens = parseInt(process.env.OPENAI_MAX_RESPONSE_TOKENS || '', 10);
  const openAiMaxTotalTokens = parseInt(process.env.OPENAI_MAX_TOTAL_TOKENS || '', 10);
  const body = JSON.parse(req.body);
  const transcript = body.transcript || '';
  const inputPassword = body.passwordToSubmitToApi || '';

  if (inputPassword === process.env.API_PASSWORD) {

    let prompt = '### START TRANSCRIPT ### ' + transcript
    const endOfTranscript = " ### END TRANSCRIPT ### Please summarize the above YouTube transcript to tell me the main point the video is trying to make. Use fewer than 100 words."
    const tokensInEndOfTranscript = encode(endOfTranscript).length;

    try {
      const encodedPrompt = encode(prompt);
      let tokenCount = encodedPrompt.length;
      let messageToPrepend = ''

      if ((tokenCount + openAiMaxResponseTokens + tokensInEndOfTranscript) > openAiMaxTotalTokens) {
        const trimmedTokens = encodedPrompt.slice(0, (openAiMaxTotalTokens - openAiMaxResponseTokens - tokensInEndOfTranscript));
        prompt = decode(trimmedTokens)
        messageToPrepend = `Request was too big to submit in its entirety; only ${openAiMaxTotalTokens - openAiMaxResponseTokens} of the original ${tokenCount} tokens could be submitted (${Number((openAiMaxTotalTokens - openAiMaxResponseTokens) / tokenCount).toLocaleString(undefined, { style: 'percent', minimumFractionDigits: 1 })}).`
      }

      prompt = prompt + endOfTranscript

      let summary: any = '';
      let apiResponseKey = 'text'

      if (process.env.OPENAI_MODEL !== "gpt-3.5-turbo") {
        const completion = await openai.createCompletion({
          model: process.env.OPENAI_MODEL,
          prompt: prompt,
          max_tokens: openAiMaxResponseTokens,
        });

        summary = completion.data.choices[0].text;
      } else {
        const completion = await openai.createChatCompletion({
          model: process.env.OPENAI_MODEL,
          messages: [{ role: "user", content: prompt }],
          max_tokens: openAiMaxResponseTokens,
        });
        
        summary = completion.data.choices[0].message.content;
      }


      res.status(200).json({
        message: messageToPrepend,
        summary
      })
    } catch (error: any) {
      if (error.response) {
        console.error(error.response.status);
        console.error(error.response.data);
        res.status(500).send(error.response.data.error.message);
      } else {
        console.error(error.message);
        res.status(500).send(error.message);
      }
    }
  } else res.status(500).send('Incorrect API password provided');
}

export default handler;
