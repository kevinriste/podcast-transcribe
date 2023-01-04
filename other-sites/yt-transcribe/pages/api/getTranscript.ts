// Next.js API route support: https://nextjs.org/docs/api-routes/introduction
import type { NextApiRequest, NextApiResponse } from 'next'
import getVideoId from 'get-video-id';
import YoutubeTranscript from 'youtube-transcript';

const handler = async (
  req: NextApiRequest,
  res: NextApiResponse
) => {
  const body = JSON.parse(req.body);
  const ytUrlInput = body.yturl || '';

  if (ytUrlInput === '') res.status(500).send('YouTube URL not provided.');

  else {
    try {
      const { id: ytVideoId } = getVideoId(ytUrlInput);

      const transcriptFromNpmVideoId = await YoutubeTranscript.fetchTranscript(ytVideoId || '', {
        lang: 'en',
        country: 'US'
      });

      const finalTranscript = transcriptFromNpmVideoId.map(transcriptPart => transcriptPart.text).join(' ');

      res.status(200).json(finalTranscript)
    } catch (error: any) {
      console.error(error);
      res.status(500).send(error.message);
    }
  }
}

export default handler;
