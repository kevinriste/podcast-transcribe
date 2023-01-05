import * as React from 'react';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import Box from '@mui/material/Box';
import Alert from '@mui/material/Alert';
import Typography from '@mui/material/Typography';
import Container from '@mui/material/Container';
import Head from 'next/head'

const Home = () => {
  const getYoutubeTranscript = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setTranscriptText('Fetching transcript...')
    setSummaryText('')
    setisTranscriptError(false)
    setIsSummaryError(false)
    const data = new FormData(event.currentTarget);
    const dataToSubmit = {
      yturl: data.get('yturl'),
    };
    const response = await fetch("/api/getTranscript", {
      method: "POST",
      body: JSON.stringify(dataToSubmit),
    });
    if (response.ok) {
      const responseJson = await response.json();
      setTranscriptText(responseJson)
    }
    else {
      const responseError = await response.text();
      console.error(responseError);
      setisTranscriptError(true)
      setTranscriptText(responseError.toString())
    }
  };

  const getTranscriptSummary = async (event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    setSummaryText('Fetching summary...')
    setIsSummaryError(false)
    const dataToSubmit = {
      transcript: transcriptText,
    };
    const response = await fetch("/api/getSummary", {
      method: "POST",
      body: JSON.stringify(dataToSubmit),
    });
    if (response.ok) {
      const responseJson = await response.json();
      setSummaryText(responseJson)
    }
    else {
      const responseError = await response.text();
      console.error(responseError);
      setIsSummaryError(true)
      setSummaryText(responseError.toString())
    }
  };

  const [isTranscriptError, setisTranscriptError] = React.useState(false);
  const [transcriptText, setTranscriptText] = React.useState('');

  const [isSummaryError, setIsSummaryError] = React.useState(false);
  const [summaryText, setSummaryText] = React.useState('');

  return (
    <Container maxWidth="lg">
      <Head>
        <title>YouTube Transcribe</title>
      </Head>
      <Box
        sx={{
          marginTop: 8,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
        }}
      >
        <Typography component="h1" variant="h5">
          Enter the YouTube URL
        </Typography>
        <Box component="form" onSubmit={getYoutubeTranscript} noValidate sx={{ mt: 1 }}>
          <TextField
            margin="normal"
            fullWidth
            id="yturl"
            label="YouTube URL"
            name="yturl"
            autoFocus
          />
          <Button
            type="submit"
            fullWidth
            variant="contained"
            sx={{ mt: 3 }}
          >
            Get transcript
          </Button>
        </Box>
        {transcriptText !== '' && <Box
          sx={{
            marginTop: 4,
            marginBottom: 4,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
          }}
        >
          {!isTranscriptError && transcriptText !== 'Fetching transcript...' &&
            <>
              <Button
                onClick={getTranscriptSummary}
                variant="outlined"
                sx={{ mb: 2 }}
              >
                Get Summary
              </Button>
              {!isSummaryError && summaryText !== '' &&
                <Typography
                  sx={{ mb: 2 }}>
                  {summaryText}
                </Typography>
              }
              {isSummaryError &&
                <Alert
                  severity="error"
                  sx={{ mb: 2 }}
                >
                  {summaryText}
                </Alert>
              }
              <Button
                onClick={() => navigator.clipboard.writeText(transcriptText)}
                variant="outlined"
                sx={{ mb: 2 }}
              >
                Copy to clipboard
              </Button>
            </>
          }
          {!isTranscriptError && <Typography>
            {transcriptText}
          </Typography>}
          {isTranscriptError && <Alert
            severity="error"
          >
            {transcriptText}
          </Alert>}
        </Box>}
      </Box>
    </Container>
  );
}

export default Home;
