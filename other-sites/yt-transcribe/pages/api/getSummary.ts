// Next.js API route support: https://nextjs.org/docs/api-routes/introduction
import type { NextApiRequest, NextApiResponse } from 'next'
const { Configuration, OpenAIApi } = require("openai");

const configuration = new Configuration({
  apiKey: process.env.OPENAI_API_KEY,
});
const openai = new OpenAIApi(configuration);

const handler = async (
  req: NextApiRequest,
  res: NextApiResponse
) => {
  const body = JSON.parse(req.body);
  const transcript = body.transcript || '';
  const prompt = 'Please summarize the below YouTube transcript to tell me the main point the video is trying to make: "' + transcript + '"'

  try {
    const completion = await openai.createCompletion({
      model: "text-davinci-003",
      prompt: prompt,
      max_tokens: 2048,
    });

    const summary = completion.data.choices[0].text;

    res.status(200).json(summary)
  } catch (error: any) {
    if (error.response) {
      console.error(error.response.status);
      console.error(error.response.data);
      res.status(500).send(JSON.stringify(error.response));
    } else {
      console.error(error.message);
      res.status(500).send(error.message);
    }
  }
}

export default handler;
