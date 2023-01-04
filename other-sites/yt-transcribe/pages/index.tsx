import * as React from 'react';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import Container from '@mui/material/Container';

const Home = () => {
  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setTranscriptText('Fetching transcript...')
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
      setTranscriptText(responseError.toString())
    }
  };

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
            sx={{ mt: 3, mb: 2 }}
          >
            Get transcript
          </Button>
        </Box>
        {transcriptText !== '' && <Box>
          <Typography component="h1" variant="h5">
            {transcriptText}
          </Typography>
        </Box>}
      </Box>
    </Container>
  );
}

export default Home;
