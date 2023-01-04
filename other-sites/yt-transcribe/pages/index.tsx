import * as React from 'react';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import Box from '@mui/material/Box';
import Alert from '@mui/material/Alert';
import Typography from '@mui/material/Typography';
import Container from '@mui/material/Container';

const Home = () => {
  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setTranscriptText('Fetching transcript...')
    setisError(false)
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
      setisError(true)
      setTranscriptText(responseError.toString())
    }
  };

  const [isError, setisError] = React.useState(false);
  const [transcriptText, setTranscriptText] = React.useState('');

  return (
    <Container maxWidth="lg">
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
        <Box component="form" onSubmit={handleSubmit} noValidate sx={{ mt: 1 }}>
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
          {!isError && transcriptText !== 'Fetching transcript...' && <Button
            onClick={() => navigator.clipboard.writeText(transcriptText)}
            variant="outlined"
            sx={{ mb: 2 }}
          >
            Copy to clipboard
          </Button>}
          {!isError && <Typography>
            {transcriptText}
          </Typography>}
          {isError && <Alert
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
